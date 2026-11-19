"""Step 6 ops: RES-09 leases (two workers), alerts (dedup/cooldown, ALERT-01/02/03/04/06), dashboard, REST API."""
import datetime as dt
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab import alerts, api, dashboard  # noqa: E402
from lab import runtime as rt  # noqa: E402
from lab.db import connect  # noqa: E402

CAPS = {"nightly_max_usd": None, "nightly_max_agent_runs": 30, "per_call_max_usd": None,
        "per_suite_max_usd": None, "soft_limit_ratio": 0.8, "hard_limit_ratio": 1.0}
T = dt.datetime(2026, 11, 19, 23, 0, tzinfo=rt.KST)


def _kinds(conn):
    return [r[0] for r in conn.execute("SELECT kind FROM alerts ORDER BY alert_id")]


# ---- RES-09 ----------------------------------------------------------------------------------
def test_res09_two_workers_never_run_the_same_step(tmp_path):
    db = tmp_path / "lab.db"
    conn = connect(db)
    jobs = [rt.enqueue(conn, "x", [{"needs_llm": False}] * 4, "t") for _ in range(3)]
    ran, lock = [], threading.Lock()

    def worker(wid):
        c = connect(db)

        def ex(step):
            with lock:
                ran.append((wid, step["job_id"], step["step_id"]))
            time.sleep(0.01)
            return {"result_ref": "ok"}
        rt.run_worker(c, ex, caps=CAPS, worker_id=wid, window_end_at=rt.utcnow() + dt.timedelta(hours=1))
        c.close()

    ts = [threading.Thread(target=worker, args=(w,)) for w in ("A", "B")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    per_step = Counter((j, s) for _, j, s in ran)
    assert len(per_step) == 12 and set(per_step.values()) == {1}  # every step exactly once
    for j in jobs:  # a job's steps all ran under one worker's lease
        assert len({w for w, jj, _ in ran if jj == j}) == 1
    assert {r[0] for r in conn.execute("SELECT status FROM jobs")} == {"COMPLETED"}
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE lease_owner IS NOT NULL").fetchone()[0] == 0  # released


def test_res09_live_lease_blocks_and_expired_lease_is_taken_over(tmp_path):
    db = tmp_path / "lab.db"
    a, b = connect(db), connect(db)
    jid = rt.enqueue(a, "x", [{"needs_llm": False}] * 2, "t", clock=lambda: T)
    assert rt.acquire_lease(a, jid, "A", T, ttl_s=600)
    a.execute("UPDATE jobs SET status='RUNNING' WHERE job_id=?", (jid,))  # A is mid-step (or about to die)
    assert not rt.acquire_lease(b, jid, "B", T)
    calls = []
    assert rt.run_worker(b, lambda s: calls.append(s["step_id"]), clock=lambda: T, caps=CAPS, worker_id="B") == "idle"
    assert calls == [] and b.execute("SELECT status FROM jobs").fetchone()[0] == "RUNNING"
    assert b.execute("SELECT COUNT(*) FROM job_events WHERE event_type='RECOVERED_ORPHAN'").fetchone()[0] == 0
    later = T + dt.timedelta(minutes=11)  # A never renewed: lease expired
    rt.run_worker(b, lambda s: calls.append(s["step_id"]), clock=lambda: later, caps=CAPS, worker_id="B")
    assert calls == ["s001", "s002"]
    assert b.execute("SELECT status, lease_owner FROM jobs").fetchone()[:] == ("COMPLETED", None)
    assert b.execute("SELECT COUNT(*) FROM job_events WHERE event_type='RECOVERED_ORPHAN'").fetchone()[0] == 1
    assert "ALERT-04" in _kinds(b)


# ---- alerts ----------------------------------------------------------------------------------
def test_alert_dedup_and_cooldown():
    conn = connect(":memory:")
    assert alerts.raise_alert(conn, "ALERT-02", "j1", "m", now=T) is True
    assert alerts.raise_alert(conn, "ALERT-02", "j1", "m", now=T + dt.timedelta(minutes=30)) is False
    assert conn.execute("SELECT COUNT(*), MAX(occurrences) FROM alerts").fetchone()[:] == (1, 2)
    alerts.mark_delivered(conn, [r["alert_id"] for r in alerts.pending(conn)], now=T)
    assert alerts.pending(conn) == []
    assert alerts.raise_alert(conn, "ALERT-02", "j1", "again", now=T + dt.timedelta(hours=2)) is True
    assert [r["message"] for r in alerts.pending(conn)] == ["again"]  # re-raised -> undelivered again


def test_alerts_on_budget_and_quota_transitions():
    conn = connect(":memory:")
    rt.enqueue(conn, "x", [{"needs_llm": True}], "t", clock=lambda: T)
    rt.run_worker(conn, lambda s: {"usd": 0.0}, clock=lambda: T, caps=dict(CAPS, nightly_max_agent_runs=0))
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "WAITING_BUDGET_APPROVAL"
    assert _kinds(conn) == ["ALERT-02"]

    conn = connect(":memory:")
    rt.enqueue(conn, "x", [{"needs_llm": True}] * 2, "t", clock=lambda: T)

    def quota(step):
        if step["step_id"] == "s002":
            raise rt.QuotaExhausted()
        return {"usd": 0.0}
    rt.run_worker(conn, quota, clock=lambda: T, caps=dict(CAPS, nightly_max_agent_runs=2, soft_limit_ratio=0.5))
    assert _kinds(conn) == ["ALERT-01", "ALERT-03"]  # soft 50% of 2 runs on the 1st reserve, then quota
    assert "미확인" in conn.execute("SELECT message FROM alerts WHERE kind='ALERT-03'").fetchone()[0]
    assert "ALERT-03" in rt.status_report(conn, now=T, caps=CAPS)


def _call(conn, cid, step, tokens, status="success"):
    conn.execute("INSERT INTO llm_calls (call_id, timestamp_utc, run_id, step_id, stage, agent_role, call_path,"
                 " usage_source, input_tokens, output_tokens, status, estimated_cost, model_id, skill_id)"
                 " VALUES (?,?,?,?,'execute','task','claude_cli','provider',?,0,?,0.01,'m1','NO_SKILL')",
                 (cid, rt.iso(T), "r1", step, tokens, status))


def test_token_spike_alert():
    conn = connect(":memory:")
    for i, n in enumerate([100, 110, 90, 1000]):
        _call(conn, f"c{i}", "execute-T01", n)
    _call(conn, "lonely", "other", 5000)  # < 3 calls in its group: no baseline, no alert
    assert alerts.check_token_spikes(conn, now=T) == ["c3"]
    assert alerts.check_token_spikes(conn, now=T) == ["c3"] and _kinds(conn) == ["ALERT-06"]  # deduped


# ---- dashboard -------------------------------------------------------------------------------
SECTIONS = ("야간 작업 관제판", "진행률", "사용량", "토큰 소비 TOP 10", "수집 상태", "실패 사례", "알림", "추정")


def test_dashboard_empty_and_with_rows():
    conn = connect(":memory:")
    page = dashboard.render_html(conn, now=T, caps=CAPS)
    assert page.startswith("<!doctype html>") and all(s in page for s in SECTIONS)
    assert "prefers-color-scheme" in page and "<script" not in page
    jid = rt.enqueue(conn, "x", [{"needs_llm": False}] * 2, "t", clock=lambda: T)
    _call(conn, "c1", "execute-T01", 1234, status="error")
    conn.execute("INSERT INTO sources (id, url, type) VALUES ('github:a/b', 'https://github.com/a/b', 'github_repo')")
    conn.execute("INSERT INTO failure_cases (category, explanation, created_at) VALUES ('TRIGGER', 'x', ?)", (rt.iso(T),))
    alerts.raise_alert(conn, "ALERT-02", jid, "<b>budget</b>", now=T)
    page = dashboard.render_html(conn, now=T, caps=CAPS)
    assert jid in page and "0/2" in page and "1234" in page and "github:a/b" in page and "TRIGGER" in page
    assert "&lt;b&gt;budget" in page  # escaped


# ---- API -------------------------------------------------------------------------------------
def _req(base, path, method="GET", body=None, token=None):
    r = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_api_get_and_token_gated_post(tmp_path):
    db = tmp_path / "lab.db"
    conn = connect(db)
    jid = rt.enqueue(conn, "x", [{"needs_llm": False}], "t", clock=lambda: T)
    srv = api.make_server(db, port=0, token="s3cret")
    off = api.make_server(db, port=0, token="")  # LAB_API_TOKEN unset
    for s in (srv, off):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    base, base_off = (f"http://127.0.0.1:{s.server_port}" for s in (srv, off))
    try:
        code, body = _req(base, "/jobs")
        assert code == 200 and json.loads(body)[0]["job_id"] == jid
        assert json.loads(_req(base, f"/jobs/{jid}")[1])["total"] == 1
        assert _req(base, f"/jobs/{jid}/usage")[0] == 200
        assert _req(base, "/reports/nightly?date=2026-11-19")[0] == 200
        assert _req(base, "/dashboard")[0] == 200
        assert _req(base, f"/jobs/{jid}/pause", "POST", {"by": "alice"})[0] == 403  # no token
        assert _req(base, f"/jobs/{jid}/pause", "POST", {"by": "alice"}, token="wrong")[0] == 403
        assert _req(base_off, f"/jobs/{jid}/pause", "POST", {"by": "alice"}, token="")[0] == 403
        assert _req(base, f"/jobs/{jid}/pause", "POST", {}, token="s3cret")[0] == 400
        assert _req(base, f"/jobs/{jid}/pause", "POST", {"by": "alice"}, token="s3cret")[0] == 200
        assert _req(base, f"/jobs/{jid}/pause", "POST", {"by": "alice"}, token="s3cret")[0] == 200  # PAUSED->PAUSED ok
        assert _req(base, "/jobs/nope/cancel", "POST", {"by": "alice"}, token="s3cret")[0] == 404
        assert _req(base, "/jobs", "POST", {"experiment_id": "e1"}, token="s3cret")[0] == 400  # approved_by required
        ev = json.loads(_req(base, f"/jobs/{jid}/events")[1])
        assert any(e["payload"].get("to") == "PAUSED" and e["payload"].get("by") == "api:alice"
                   for e in ev if isinstance(e["payload"], dict))
    finally:
        for s in (srv, off):
            s.shutdown()
            s.server_close()
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "PAUSED"
