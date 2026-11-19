"""Budget Guard (PRD 19.6, 24): code rules only, never an LLM decision.

Reserve before a call, settle after. Caps come from config/budgets.yaml and are
never raised here - changing a cap means a human edits the config.
"""
import json
import uuid

import yaml

from lab.db import ROOT, now, tx

# scope -> (usd cap key, run cap key). Unknown cost is never free: the run cap always applies.
SCOPE_KEYS = {
    "nightly_window": ("nightly_max_usd", "nightly_max_agent_runs"),
    "daily": ("daily_max_usd", "nightly_max_agent_runs"),  # ponytail: no daily run cap in config; reuse nightly one
    "per_suite": ("per_suite_max_usd", "nightly_max_agent_runs"),
}


def load_caps(path=ROOT / "config" / "budgets.yaml"):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["budgets"]


def _spent(conn, scope, scope_key):
    """reserved + settled (released rows count 0)."""
    r = conn.execute(
        "SELECT COALESCE(SUM(CASE status WHEN 'reserved' THEN reserved_usd WHEN 'settled' THEN settled_usd ELSE 0 END),0),"
        " COALESCE(SUM(CASE WHEN status IN ('reserved','settled') THEN reserved_runs ELSE 0 END),0)"
        " FROM token_reservations WHERE scope=? AND scope_key=?", (scope, scope_key)).fetchone()
    return float(r[0]), int(r[1])


def _event(conn, job_id, event_type, payload):
    conn.execute("INSERT INTO job_events (job_id, event_type, payload_redacted, created_at) VALUES (?,?,?,?)",
                 (job_id or "budget", event_type, json.dumps(payload, ensure_ascii=False), now()))


def reserve(conn, scope, scope_key, usd=None, runs=1, job_id=None, caps=None):
    """-> (reservation_id | None, block_reason | None, soft_warning: bool). Atomic (BEGIN IMMEDIATE)."""
    caps = caps or load_caps()
    usd_key, run_key = SCOPE_KEYS[scope]
    usd_cap, run_cap = caps.get(usd_key), caps.get(run_key)
    hard, soft = caps.get("hard_limit_ratio", 1.0), caps.get("soft_limit_ratio", 0.8)
    with tx(conn):
        spent_usd, spent_runs = _spent(conn, scope, scope_key)
        reason = None
        if usd_cap is not None and usd is None:
            reason = f"{usd_key}={usd_cap} is set but this call has no cost estimate (unknown cost is not free)"
        elif usd_cap is not None and spent_usd + usd > usd_cap * hard + 1e-9:
            reason = f"hard limit {usd_key}: spent {spent_usd:.4f} + {usd:.4f} > {usd_cap}"
        elif run_cap is not None and spent_runs + runs > run_cap * hard:
            reason = f"hard limit {run_key}: {spent_runs} + {runs} > {run_cap}"
        if reason:
            _event(conn, job_id, "BUDGET_BLOCKED", {"scope": scope, "scope_key": scope_key, "reason": reason})
            return None, reason, False
        rid = "res-" + uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO token_reservations (reservation_id, job_id, scope, scope_key, reserved_usd,"
                     " reserved_runs, status, created_at) VALUES (?,?,?,?,?,?,'reserved',?)",
                     (rid, job_id, scope, scope_key, usd or 0.0, runs, now()))
        warn = ((usd_cap is not None and spent_usd + (usd or 0) >= usd_cap * soft - 1e-9) or
                (run_cap is not None and spent_runs + runs >= run_cap * soft))
        if warn:
            _event(conn, job_id, "ALERT-01_BUDGET_SOFT_LIMIT",
                   {"scope": scope, "scope_key": scope_key, "usd": spent_usd + (usd or 0), "usd_cap": usd_cap,
                    "runs": spent_runs + runs, "run_cap": run_cap, "ratio": soft})
        return rid, None, warn


def try_reserve(conn, scope, scope_key, usd=None, runs=1, job_id=None, caps=None):
    """-> reservation_id, or None on a hard limit."""
    return reserve(conn, scope, scope_key, usd, runs, job_id, caps)[0]


def settle(conn, reservation_id, actual_usd):
    """actual_usd=None -> keep the reserved amount as (conservative) spend, note 'usage unavailable'."""
    with tx(conn):
        row = conn.execute("SELECT job_id, reserved_usd, status FROM token_reservations WHERE reservation_id=?",
                           (reservation_id,)).fetchone()
        if row is None or row["status"] != "reserved":
            return
        usd = row["reserved_usd"] if actual_usd is None else actual_usd
        conn.execute("UPDATE token_reservations SET status='settled', settled_usd=? WHERE reservation_id=?",
                     (usd, reservation_id))
        if actual_usd is None:
            _event(conn, row["job_id"], "BUDGET_SETTLED",
                   {"reservation_id": reservation_id, "settled_usd": usd, "note": "usage unavailable"})


def release(conn, reservation_id):
    """Call never started -> free the reservation."""
    with tx(conn):
        conn.execute("UPDATE token_reservations SET status='released' WHERE reservation_id=? AND status='reserved'",
                     (reservation_id,))


def remaining(conn, scope_key, caps=None, scope="nightly_window"):
    caps = caps or load_caps()
    usd_key, run_key = SCOPE_KEYS[scope]
    spent_usd, spent_runs = _spent(conn, scope, scope_key)
    usd_cap, run_cap = caps.get(usd_key), caps.get(run_key)
    return {"spent_usd": spent_usd, "spent_runs": spent_runs,
            "remaining_usd": None if usd_cap is None else max(0.0, usd_cap - spent_usd),
            "remaining_runs": None if run_cap is None else max(0, run_cap - spent_runs)}


def caps_for_next_call(conn, scope_key, caps=None):
    """Values the caller passes on: claude -p --max-budget-usd / claude plugin eval --max-cost-usd.
    None = no USD cap configured (run-count cap still enforced by reserve)."""
    caps = caps or load_caps()
    rem = remaining(conn, scope_key, caps)

    def tighter(a, b):
        return b if a is None else a if b is None else min(a, b)

    return {"max_budget_usd": tighter(caps.get("per_call_max_usd"), rem["remaining_usd"]),
            "max_cost_usd": tighter(caps.get("per_suite_max_usd"), rem["remaining_usd"]),
            "remaining_usd": rem["remaining_usd"], "remaining_runs": rem["remaining_runs"]}
