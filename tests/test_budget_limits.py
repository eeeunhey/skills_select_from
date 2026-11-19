"""Budget Guard (PRD 19.6): RES-08 soft/hard limits, atomic reservations, null USD cap still counts runs."""
import datetime as dt
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab import budget, runtime as rt  # noqa: E402
from lab.db import connect  # noqa: E402

BASE = {"per_call_max_usd": None, "per_suite_max_usd": None, "daily_max_usd": None,
        "soft_limit_ratio": 0.8, "hard_limit_ratio": 1.0}


def caps(**kw):
    return {**BASE, "nightly_max_usd": None, "nightly_max_agent_runs": 30, **kw}


def events(conn, t):
    return conn.execute("SELECT COUNT(*) FROM job_events WHERE event_type=?", (t,)).fetchone()[0]


def test_res08_soft_warning_then_hard_block_and_caps_not_raised():
    conn = connect(":memory:")
    c = caps(nightly_max_usd=1.0)
    snapshot = dict(c)
    r1, _, w1 = budget.reserve(conn, "nightly_window", "2026-11-19", 0.5, caps=c)
    r2, _, w2 = budget.reserve(conn, "nightly_window", "2026-11-19", 0.3, caps=c)
    r3, why, _ = budget.reserve(conn, "nightly_window", "2026-11-19", 0.3, caps=c)
    assert r1 and r2 and not w1 and w2 and r3 is None and "hard limit" in why
    assert events(conn, "ALERT-01_BUDGET_SOFT_LIMIT") == 1 and events(conn, "BUDGET_BLOCKED") == 1
    assert c == snapshot and budget.load_caps()["nightly_max_usd"] is None  # nothing auto-raised
    # another key (next night) is a fresh window
    assert budget.try_reserve(conn, "nightly_window", "2026-11-20", 0.3, caps=c)


def test_settle_and_release():
    conn = connect(":memory:")
    c = caps(nightly_max_usd=1.0)
    r = budget.try_reserve(conn, "nightly_window", "k", 0.6, caps=c)
    budget.settle(conn, r, 0.1)
    assert budget.remaining(conn, "k", c)["remaining_usd"] == 0.9
    r = budget.try_reserve(conn, "nightly_window", "k", 0.4, caps=c)
    budget.settle(conn, r, None)  # usage unavailable -> keep reserved amount
    assert abs(budget.remaining(conn, "k", c)["remaining_usd"] - 0.5) < 1e-9
    assert events(conn, "BUDGET_SETTLED") == 1
    r = budget.try_reserve(conn, "nightly_window", "k", 0.5, caps=c)
    budget.release(conn, r)
    assert abs(budget.remaining(conn, "k", c)["remaining_usd"] - 0.5) < 1e-9
    nxt = budget.caps_for_next_call(conn, "k", {**c, "per_call_max_usd": 0.2})
    assert nxt["max_budget_usd"] == 0.2 and abs(nxt["max_cost_usd"] - 0.5) < 1e-9


def test_null_usd_cap_still_enforces_run_count():
    conn = connect(":memory:")
    c = caps(nightly_max_agent_runs=2)
    assert budget.try_reserve(conn, "nightly_window", "k", None, caps=c)
    assert budget.try_reserve(conn, "nightly_window", "k", None, caps=c)
    assert budget.try_reserve(conn, "nightly_window", "k", None, caps=c) is None
    assert budget.caps_for_next_call(conn, "k", c) == {"max_budget_usd": None, "max_cost_usd": None,
                                                        "remaining_usd": None, "remaining_runs": 0}


def test_unknown_cost_is_not_free_when_usd_cap_set():
    conn = connect(":memory:")
    rid, why, _ = budget.reserve(conn, "nightly_window", "k", None, caps=caps(nightly_max_usd=1.0))
    assert rid is None and "no cost estimate" in why


def test_concurrent_reservations_never_exceed_cap(tmp_path):
    db = tmp_path / "lab.db"
    connect(db).close()
    c = caps(nightly_max_usd=1.0, nightly_max_agent_runs=100)
    got = []

    def worker():
        conn = connect(db)
        for _ in range(10):
            if budget.try_reserve(conn, "nightly_window", "k", 0.3, caps=c):
                got.append(1)
        conn.close()

    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    conn = connect(db)
    assert len(got) == 3
    assert budget.remaining(conn, "k", c)["spent_usd"] <= 1.0


def test_worker_blocks_on_budget_and_approve_does_not_raise_cap():
    conn = connect(":memory:")
    c = caps(nightly_max_usd=0.5)
    t = dt.datetime(2026, 11, 19, 23, 0, tzinfo=rt.KST)
    jid = rt.enqueue(conn, "eval", [{"estimated_usd": 0.4}, {"estimated_usd": 0.4}], "t", clock=lambda: t)
    calls = []
    rt.run_worker(conn, lambda s: calls.append(s["caps"]) or {"usd": 0.4}, clock=lambda: t, caps=c)
    assert len(calls) == 1 and abs(calls[0]["max_budget_usd"] - 0.1) < 1e-9  # remaining after this reservation
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "WAITING_BUDGET_APPROVAL"
    assert "hard limit" in rt.status_report(conn, t, caps=c, cfg=rt.load_runtime())
    rt.approve_budget(conn, jid, "operator", clock=lambda: t)
    rt.run_worker(conn, lambda s: calls.append(1), clock=lambda: t, caps=c)
    assert len(calls) == 1  # still blocked: approval is not a cap increase
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "WAITING_BUDGET_APPROVAL"
