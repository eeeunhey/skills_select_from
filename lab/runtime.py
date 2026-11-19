"""Reduced nightly orchestrator (PRD 20, 25.1 step 5): one worker, SQLite queue, checkpoints,
05:00 KST deadline, quota/rate-limit waiting. All state transitions are code rules (PRD 24).

ponytail: single worker, no lease/heartbeat. A job found RUNNING at pick time is an orphan of a
killed process and is re-run from its last committed step. Multi-worker needs leases (step 6).
"""
import argparse
import datetime as dt
import json
import random
import re
import sys
import time

import yaml

from lab import budget
from lab.db import ROOT, connect, tx

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:  # ponytail: no tzdata on this Python; Korea has no DST so fixed +9 is exact
    KST = dt.timezone(dt.timedelta(hours=9), "KST")

UTC = dt.timezone.utc
TERMINAL = ("COMPLETED", "FAILED_PERMANENT", "CANCELLED")


# ---- errors the executor (or the classifier) raises ------------------------------------------
class RecoverableError(Exception):
    """kind: rate_limit | transient. attempts_used counts SDK/CLI internal retries too."""
    def __init__(self, kind="transient", retry_after=None, attempts_used=1, usd=None, msg=""):
        super().__init__(msg or kind)
        self.kind, self.retry_after, self.attempts_used, self.usd = kind, retry_after, attempts_used, usd


class QuotaExhausted(Exception):
    """Plan/credit/quota limit. reset_at = confirmed reset time (aware dt) or None (unknown -> never call)."""
    def __init__(self, reset_at=None, usd=None, msg="quota exhausted"):
        super().__init__(msg)
        self.reset_at, self.usd = reset_at, usd


class BudgetExceeded(Exception):
    def __init__(self, usd=None, msg="budget exceeded"):
        super().__init__(msg)
        self.usd = usd


class PermanentError(Exception):
    def __init__(self, usd=None, msg="permanent error"):
        super().__init__(msg)
        self.usd = usd


def _normalize(e, classifier):
    """Map any exception to one of the four types above. classifier(e) may return one of those
    exceptions or a state string (lab.ledger.classify_error style)."""
    if isinstance(e, (RecoverableError, QuotaExhausted, BudgetExceeded, PermanentError)):
        return e
    c = classifier(e) if classifier else None
    if isinstance(c, Exception):
        return c
    usd = getattr(e, "usd", None)
    if c == "WAITING_RATE_LIMIT":
        return RecoverableError("rate_limit", getattr(e, "retry_after", None), getattr(e, "attempts_used", 1), usd, str(e))
    if c == "transient":
        return RecoverableError("transient", None, getattr(e, "attempts_used", 1), usd, str(e))
    if c == "WAITING_QUOTA_RESET":
        return QuotaExhausted(getattr(e, "reset_at", None), usd, str(e))
    if c == "WAITING_BUDGET_APPROVAL":
        return BudgetExceeded(usd, str(e))
    return PermanentError(usd, f"{type(e).__name__}: {e}")  # unknown -> stop, don't burn money retrying


# ---- config / clock / window ---------------------------------------------------------------
def load_runtime(path=ROOT / "config" / "runtime.yaml"):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def utcnow():
    return dt.datetime.now(UTC)


def iso(t):
    return t.astimezone(UTC).isoformat() if t else None


def parse(s):
    return dt.datetime.fromisoformat(s) if s else None


def window_end(start, until="05:00"):
    """Next 05:00 Asia/Seoul after `start` (aware). 23:00 Nov 19 -> 05:00 Nov 20; 01:00 Nov 20 -> 05:00 Nov 20."""
    h, m = map(int, until.split(":"))
    local = start.astimezone(KST)
    end = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if end <= local:
        end += dt.timedelta(days=1)
    return end


def near_end(now, end, minutes=5):
    return now >= end - dt.timedelta(minutes=minutes)


def night_key(end):
    """Budget scope_key of a window = KST date the night started (window ending 11-20 05:00 -> '2026-11-19')."""
    return (end.astimezone(KST) - dt.timedelta(days=1)).date().isoformat()


# ---- events / transitions ------------------------------------------------------------------
_SECRET_KEY = re.compile(r"key|token|secret|passw|auth|cookie", re.I)
_SECRET_VAL = re.compile(r"(sk-ant-[A-Za-z0-9_\-]+|sk-[A-Za-z0-9]{16,}|Bearer\s+\S+)")


def redact(obj):
    if isinstance(obj, dict):
        return {k: "[REDACTED]" if _SECRET_KEY.search(k) and k not in ("idempotency_key",) else redact(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return _SECRET_VAL.sub("[REDACTED]", obj)[:2000]
    return obj


def _event(conn, job_id, event_type, payload, at):
    conn.execute("INSERT INTO job_events (job_id, event_type, payload_redacted, created_at) VALUES (?,?,?,?)",
                 (job_id, event_type, json.dumps(redact(payload), ensure_ascii=False, default=str), iso(at)))


def _set(conn, job_id, status, at, reason=None, next_eligible_at=None, window_end_at=None, by=None, **extra):
    """Only place job status changes. Call inside `with tx(conn)`."""
    old = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()["status"]
    conn.execute("UPDATE jobs SET status=?, next_eligible_at=?, window_end_at=COALESCE(?, window_end_at),"
                 " updated_at=? WHERE job_id=?", (status, iso(next_eligible_at), iso(window_end_at), iso(at), job_id))
    _event(conn, job_id, "STATUS", {"from": old, "to": status, "reason": reason, "by": by,
                                    "next_eligible_at": iso(next_eligible_at), **extra}, at)


# ---- queue ---------------------------------------------------------------------------------
def enqueue(conn, kind, steps, approved_by, payload=None, job_id=None, workflow_version="v1", clock=utcnow):
    """steps: [{step_id?, needs_llm?, may_run_long?, estimated_usd?, idempotency_key?, payload?}] in order."""
    if not approved_by:
        raise ValueError("jobs only run inside an approved scope (PRD 24): approved_by is required")
    at = clock()
    job_id = job_id or f"job-{at.astimezone(KST):%Y%m%d-%H%M%S}-{random.randrange(16**4):04x}"
    with tx(conn):
        conn.execute("INSERT INTO jobs (job_id, kind, status, approved_by, workflow_version, payload_json, created_at,"
                     " updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     (job_id, kind, "QUEUED", approved_by, workflow_version,
                      json.dumps(payload or {}, ensure_ascii=False), iso(at), iso(at)))
        for seq, s in enumerate(steps, 1):
            sid = s.get("step_id", f"s{seq:03d}")
            conn.execute("INSERT INTO job_steps (step_id, job_id, seq, status, may_run_long, needs_llm, estimated_usd,"
                         " idempotency_key, payload_json) VALUES (?,?,?,'PENDING',?,?,?,?,?)",
                         (sid, job_id, seq, int(s.get("may_run_long", True)), int(s.get("needs_llm", True)),
                          s.get("estimated_usd"), s.get("idempotency_key", f"{job_id}:{sid}"),
                          json.dumps(s.get("payload", {}), ensure_ascii=False)))
        _event(conn, job_id, "ENQUEUED", {"kind": kind, "steps": len(steps), "by": approved_by}, at)
    return job_id


def _eligible(job, now, end, cfg):
    st = job["status"]
    if not job["approved_by"] or st in TERMINAL:
        return False
    if st in ("QUEUED", "CHECKPOINTED", "RUNNING"):  # RUNNING here = orphan of a killed worker
        return True
    nxt = parse(job["next_eligible_at"])
    if st in ("WAITING_RATE_LIMIT", "WAITING_QUOTA_RESET"):  # quota: only with a confirmed reset time
        return nxt is not None and nxt <= now
    if st == "PAUSED_WINDOW_CLOSED":
        prev = parse(job["window_end_at"])
        return bool(cfg["runtime"].get("resume_next_window")) and (prev is None or prev < end)
    return False  # WAITING_BUDGET_APPROVAL, WAITING_SECURITY_APPROVAL, PAUSED: human only


def _next_wake(conn, now, end):
    r = conn.execute("SELECT MIN(next_eligible_at) FROM jobs WHERE approved_by IS NOT NULL AND"
                     " status IN ('WAITING_RATE_LIMIT','WAITING_QUOTA_RESET') AND next_eligible_at IS NOT NULL"
                     ).fetchone()[0]
    t = parse(r)
    return t if t and t < end else None


def commit_step(conn, job_id, step_id, idempotency_key, result_ref, at):
    """ONE transaction: step DONE + result_ref + checkpoint + job CHECKPOINTED + event.
    Idempotent: a second commit of the same idempotency_key is a no-op (returns False)."""
    with tx(conn):
        cur = conn.execute("UPDATE job_steps SET status='DONE', result_ref=? WHERE job_id=? AND step_id=?"
                           " AND idempotency_key=? AND status='PENDING'", (result_ref, job_id, step_id, idempotency_key))
        if cur.rowcount == 0:
            return False
        done = [r[0] for r in conn.execute("SELECT step_id FROM job_steps WHERE job_id=? AND status='DONE' ORDER BY seq",
                                           (job_id,))]
        nxt = conn.execute("SELECT step_id FROM job_steps WHERE job_id=? AND status='PENDING' ORDER BY seq LIMIT 1",
                           (job_id,)).fetchone()
        conn.execute("INSERT INTO checkpoints (checkpoint_id, job_id, step_id, cursor, artifact_ref, state_json,"
                     " created_at) VALUES (?,?,?,?,?,?,?)",
                     ("cp-" + idempotency_key, job_id, step_id, nxt[0] if nxt else None, result_ref,
                      json.dumps({"completed_step_ids": done, "current_step_id": nxt[0] if nxt else None,
                                  "last_success_at_utc": iso(at), "status": "CHECKPOINTED"}), iso(at)))
        st = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
        if st == "RUNNING":  # cancelled/paused by an operator mid-step: keep the result, keep their status
            _set(conn, job_id, "CHECKPOINTED", at, reason=f"step {step_id} committed", step_id=step_id)
        else:
            _event(conn, job_id, "STEP_COMMITTED", {"step_id": step_id, "status_kept": st}, at)
    return True


def _backoff(n, jitter):
    return min(300.0, 2.0 ** n) + jitter()  # n = attempts so far; 2,4,8.. s capped at 5 min, + up to 1 s jitter


def _run_step(conn, job, step, executor, now, end, cfg, caps, classifier, on_usage, clock, jitter):
    jid, sid = job["job_id"], step["step_id"]
    max_attempts = cfg["retry"]["transient_max_attempts"]
    rid = None
    if step["attempt_count"] >= max_attempts:  # e.g. killed mid-step over and over
        with tx(conn):
            conn.execute("UPDATE job_steps SET status='FAILED' WHERE job_id=? AND step_id=?", (jid, sid))
            _set(conn, jid, "FAILED_PERMANENT", now, reason=f"step {sid}: {max_attempts} attempts used", step_id=sid)
        return
    if step["needs_llm"]:
        rid, reason, _ = budget.reserve(conn, "nightly_window", night_key(end), step["estimated_usd"], 1, jid, caps)
        if rid is None:
            with tx(conn):
                _set(conn, jid, "WAITING_BUDGET_APPROVAL", now, reason=reason, step_id=sid)
            return
    with tx(conn):  # attempt counted BEFORE the call: a kill mid-step still counts (bounded re-runs)
        conn.execute("UPDATE job_steps SET attempt_count=attempt_count+1 WHERE job_id=? AND step_id=?", (jid, sid))
        _set(conn, jid, "RUNNING", now, reason=f"step {sid} attempt {step['attempt_count'] + 1}",
             window_end_at=end, step_id=sid)
    attempts = step["attempt_count"] + 1
    call = dict(step, payload=json.loads(step["payload_json"]), job_id=jid,
                caps=budget.caps_for_next_call(conn, night_key(end), caps) if step["needs_llm"] else None)
    try:
        result = executor(call)
    except Exception as raw:
        err = _normalize(raw, classifier)
        if on_usage:
            on_usage(call, err)
        if rid:
            budget.settle(conn, rid, err.usd)  # failed attempts are spend too (OBS-06)
        at = clock()
        with tx(conn):
            if isinstance(err, RecoverableError):
                attempts += max(0, err.attempts_used - 1)
                conn.execute("UPDATE job_steps SET attempt_count=? WHERE job_id=? AND step_id=?", (attempts, jid, sid))
                if attempts >= max_attempts:
                    conn.execute("UPDATE job_steps SET status='FAILED' WHERE job_id=? AND step_id=?", (jid, sid))
                    _set(conn, jid, "FAILED_PERMANENT", at, reason=f"{err.kind}: {attempts}/{max_attempts} attempts used",
                         step_id=sid)
                else:
                    wait = err.retry_after if err.retry_after is not None else _backoff(attempts, jitter)
                    _set(conn, jid, "WAITING_RATE_LIMIT", at, reason=f"{err.kind}: {err}; retry in {wait:.0f}s",
                         next_eligible_at=at + dt.timedelta(seconds=wait), step_id=sid)
            elif isinstance(err, QuotaExhausted):  # no retry, no key switching, no paid fallback
                _set(conn, jid, "WAITING_QUOTA_RESET", at,
                     reason=f"{err}; " + ("reset confirmed" if err.reset_at else "reset time unknown - waiting for operator"),
                     next_eligible_at=err.reset_at, step_id=sid, eval_status="unevaluated")
            elif isinstance(err, BudgetExceeded):
                _set(conn, jid, "WAITING_BUDGET_APPROVAL", at, reason=str(err), step_id=sid)
            else:
                conn.execute("UPDATE job_steps SET status='FAILED' WHERE job_id=? AND step_id=?", (jid, sid))
                _set(conn, jid, "FAILED_PERMANENT", at, reason=str(err), step_id=sid)
        return
    if on_usage:
        on_usage(call, result)
    ref = result.get("result_ref") if isinstance(result, dict) else result
    commit_step(conn, jid, sid, step["idempotency_key"],
                ref if isinstance(ref, str) or ref is None else json.dumps(ref, default=str), clock())
    if rid:
        budget.settle(conn, rid, result.get("usd") if isinstance(result, dict) else None)


def run_worker(conn, executor, clock=utcnow, classifier=None, sleep=time.sleep, max_iterations=10_000,
               on_usage=None, cfg=None, caps=None, window_end_at=None, jitter=random.random):
    """Single worker loop (PRD 20.6). Returns 'idle' | 'window_closed' | 'max_iterations'.
    sleep=None -> never wait for rate-limit/quota timers (run-once)."""
    cfg = cfg or load_runtime()
    caps = caps or budget.load_caps()
    rt = cfg["runtime"]
    end = window_end_at or window_end(clock(), rt.get("work_until_local", "05:00"))
    cutoff = rt.get("stop_scheduling_new_long_steps_before_minutes", 5)
    for _ in range(max_iterations):
        now = clock()
        if now >= end:  # after 05:00: no new calls at all; in-progress jobs keep their checkpoints
            with tx(conn):
                for j in conn.execute("SELECT job_id FROM jobs WHERE status IN ('CHECKPOINTED','RUNNING')").fetchall():
                    _set(conn, j[0], "PAUSED_WINDOW_CLOSED", now, reason="05:00 KST window closed", window_end_at=end)
            return "window_closed"
        jobs = conn.execute("SELECT * FROM jobs ORDER BY created_at, job_id").fetchall()
        job = next((j for j in jobs if _eligible(j, now, end, cfg)), None)
        if job is None:
            wake = _next_wake(conn, now, end)
            if wake is None or sleep is None:
                return "idle"
            sleep(max(0.0, (wake - now).total_seconds()))
            continue
        if job["status"] == "RUNNING":
            with tx(conn):
                _event(conn, job["job_id"], "RECOVERED_ORPHAN", {"note": "worker restarted; resume from last checkpoint"}, now)
        step = conn.execute("SELECT * FROM job_steps WHERE job_id=? AND status='PENDING' ORDER BY seq LIMIT 1",
                            (job["job_id"],)).fetchone()
        if step is None:
            with tx(conn):
                _set(conn, job["job_id"], "COMPLETED", now, reason="all steps done")
            continue
        if step["may_run_long"] and near_end(now, end, cutoff):
            with tx(conn):
                _set(conn, job["job_id"], "PAUSED_WINDOW_CLOSED", now, window_end_at=end, step_id=step["step_id"],
                     reason=f"long step not started within {cutoff} min of 05:00 KST")
            continue
        _run_step(conn, job, step, executor, now, end, cfg, caps, classifier, on_usage, clock, jitter)
    return "max_iterations"


# ---- operator controls ---------------------------------------------------------------------
def _operator(conn, job_id, by, to, allowed_from, clock=utcnow):
    with tx(conn):
        row = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"no job {job_id}")
        if row["status"] not in allowed_from:
            raise ValueError(f"{job_id} is {row['status']}; cannot move to {to}")
        _set(conn, job_id, to, clock(), reason="operator", by=by)


_LIVE = ("QUEUED", "RUNNING", "CHECKPOINTED", "WAITING_RATE_LIMIT", "WAITING_QUOTA_RESET", "WAITING_BUDGET_APPROVAL",
         "WAITING_SECURITY_APPROVAL", "WAITING_DEPENDENCY", "PAUSED_WINDOW_CLOSED", "PAUSED")


def pause(conn, job_id, by, clock=utcnow):
    _operator(conn, job_id, by, "PAUSED", _LIVE, clock)


def resume(conn, job_id, by, clock=utcnow):
    """Human resume of any paused/waiting job (incl. confirmed quota reset, security approval). Never CANCELLED."""
    _operator(conn, job_id, by, "QUEUED", [s for s in _LIVE if s not in ("QUEUED", "RUNNING")], clock)


def approve_budget(conn, job_id, by, clock=utcnow):
    """Acknowledges the block; does NOT raise any cap. Edit config/budgets.yaml first or it blocks again."""
    _operator(conn, job_id, by, "QUEUED", ("WAITING_BUDGET_APPROVAL",), clock)


def cancel(conn, job_id, by, clock=utcnow):
    _operator(conn, job_id, by, "CANCELLED", _LIVE, clock)


# ---- status (CLI replaces the dashboard until step 6) --------------------------------------
def status_report(conn, now=None, caps=None, cfg=None):
    now = now or utcnow()
    cfg = cfg or load_runtime()
    end = window_end(now, cfg["runtime"].get("work_until_local", "05:00"))
    kst = lambda s: parse(s).astimezone(KST).strftime("%m-%d %H:%M:%S KST") if s else "-"
    rem = budget.remaining(conn, night_key(end), caps)
    usd_left = "no USD cap" if rem["remaining_usd"] is None else f"${rem['remaining_usd']:.4f}"
    lines = [f"now {now.astimezone(KST):%Y-%m-%d %H:%M KST} | window ends {end:%Y-%m-%d %H:%M KST}",
             f"budget night {night_key(end)}: spent ${rem['spent_usd']:.4f}, runs {rem['spent_runs']}; "
             f"remaining {usd_left}, runs {rem['remaining_runs']}"]
    counts = conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY status").fetchall()
    lines.append("jobs: " + (", ".join(f"{s}={n}" for s, n in counts) or "none"))
    for j in conn.execute("SELECT * FROM jobs WHERE status NOT IN ('COMPLETED') ORDER BY created_at").fetchall():
        done, total = conn.execute("SELECT SUM(status='DONE'), COUNT(*) FROM job_steps WHERE job_id=?",
                                   (j["job_id"],)).fetchone()
        cp = conn.execute("SELECT step_id, created_at FROM checkpoints WHERE job_id=? ORDER BY created_at DESC, rowid DESC"
                          " LIMIT 1", (j["job_id"],)).fetchone()
        ev = conn.execute("SELECT payload_redacted FROM job_events WHERE job_id=? AND event_type='STATUS'"
                          " ORDER BY event_id DESC LIMIT 1", (j["job_id"],)).fetchone()
        why = json.loads(ev[0]).get("reason") if ev else None
        lines.append(f"- {j['job_id']} [{j['status']}] steps {done or 0}/{total} | last checkpoint "
                     f"{(cp['step_id'] + ' @ ' + kst(cp['created_at'])) if cp else '-'} | next eligible "
                     f"{kst(j['next_eligible_at'])}\n    why: {why or '-'}\n    resumes: {_resume_hint(j, cfg)}")
    return "\n".join(lines)


def _resume_hint(j, cfg):
    st = j["status"]
    return {
        "WAITING_RATE_LIMIT": "automatically at next eligible time",
        "WAITING_QUOTA_RESET": "automatically at confirmed reset time" if j["next_eligible_at"]
        else "never automatically: confirm the reset, then `resume`",
        "WAITING_BUDGET_APPROVAL": "after a human edits config/budgets.yaml and runs `approve-budget`",
        "WAITING_SECURITY_APPROVAL": "only after human security approval (`resume`)",
        "PAUSED_WINDOW_CLOSED": "next night window automatically" if cfg["runtime"].get("resume_next_window")
        else "after `resume`",
        "PAUSED": "after `resume`",
        "CANCELLED": "never", "FAILED_PERMANENT": "never (fix cause, enqueue a new job)",
    }.get(st, "next worker run")


# ---- CLI -----------------------------------------------------------------------------------
def _dry(step):
    print(f"[dry] {step['job_id']} {step['step_id']} caps={step['caps']}")
    return {"result_ref": f"dry:{step['step_id']}", "usd": 0.0}


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m lab.runtime")
    p.add_argument("--db", default=None, help="SQLite path (default data/lab.db)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for name in ("run-once", "run"):
        r = sub.add_parser(name, help="run-once: no waiting on timers; run: wait until 05:00 KST")
        r.add_argument("--executor", choices=["dry"], required=True,
                       help="dry = echo steps, no LLM (real evaluator executor is wired by integration)")
    for name in ("pause", "resume", "cancel", "approve-budget"):
        o = sub.add_parser(name)
        o.add_argument("job_id")
        o.add_argument("--by", required=True)
    a = p.parse_args(argv)
    conn = connect(a.db) if a.db else connect()
    if a.cmd == "status":
        print(status_report(conn))
    elif a.cmd in ("run-once", "run"):
        print(run_worker(conn, _dry, sleep=None if a.cmd == "run-once" else time.sleep))
        print(status_report(conn))
    else:
        try:
            {"pause": pause, "resume": resume, "cancel": cancel, "approve-budget": approve_budget}[a.cmd](conn, a.job_id, a.by)
        except ValueError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        print(f"{a.cmd}: {a.job_id} by {a.by}")


if __name__ == "__main__":
    sys.exit(main())
