"""RES-03 (429 + Retry-After), RES-04 (quota exhausted), RES-17 (plan limit, no paid fallback),
bounded attempts incl. internal CLI/SDK retries, every attempt's usage reported (OBS-06)."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab import runtime as rt  # noqa: E402
from lab.db import connect  # noqa: E402

CAPS = {"nightly_max_usd": None, "nightly_max_agent_runs": 30, "per_call_max_usd": None,
        "per_suite_max_usd": None, "soft_limit_ratio": 0.8, "hard_limit_ratio": 1.0}


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 11, 19, 23, 0, tzinfo=rt.KST)
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += dt.timedelta(seconds=s)


def status(conn):
    return conn.execute("SELECT status FROM jobs").fetchone()[0]


def test_res03_retry_after_respected_then_resumes():
    conn, clock = connect(":memory:"), Clock()
    rt.enqueue(conn, "eval", [{}], "t", clock=clock)
    calls, usage = [], []

    def ex(step):
        calls.append(clock())
        if len(calls) <= 2:
            raise rt.RecoverableError("rate_limit", retry_after=30, usd=0.01)
        return {"result_ref": "ok", "usd": 0.02}

    rt.run_worker(conn, ex, clock=clock, sleep=clock.sleep, caps=CAPS, on_usage=lambda s, o: usage.append(o))
    assert clock.slept == [30, 30]
    assert [(b - a).total_seconds() for a, b in zip(calls, calls[1:])] == [30, 30]
    assert status(conn) == "COMPLETED" and len(usage) == 3  # failed attempts reported too
    spent = conn.execute("SELECT SUM(settled_usd) FROM token_reservations").fetchone()[0]
    assert abs(spent - 0.04) < 1e-9


def test_res03_bounded_no_infinite_loop():
    conn, clock = connect(":memory:"), Clock()
    rt.enqueue(conn, "eval", [{}], "t", clock=clock)
    calls = []

    def always_429(step):
        calls.append(1)
        raise rt.RecoverableError("rate_limit", retry_after=10)

    assert rt.run_worker(conn, always_429, clock=clock, sleep=clock.sleep, caps=CAPS) == "idle"
    assert len(calls) == 5 and status(conn) == "FAILED_PERMANENT"


def test_backoff_without_retry_after_and_internal_retries_count():
    conn, clock = connect(":memory:"), Clock()
    rt.enqueue(conn, "eval", [{}], "t", clock=clock)
    calls = []

    def ex(step):
        calls.append(1)
        raise rt.RecoverableError("transient", attempts_used=3)  # CLI already retried twice inside

    rt.run_worker(conn, ex, clock=clock, sleep=clock.sleep, caps=CAPS, jitter=lambda: 0.5)
    assert len(calls) == 2 and status(conn) == "FAILED_PERMANENT"  # 3 + 3 >= 5
    assert clock.slept == [2 ** 3 + 0.5]


def test_res04_quota_unknown_reset_never_calls_again():
    conn, clock = connect(":memory:"), Clock()
    jid = rt.enqueue(conn, "eval", [{}, {}], "t", clock=clock)
    calls = []

    def ex(step):
        calls.append(1)
        raise rt.QuotaExhausted(reset_at=None)

    assert rt.run_worker(conn, ex, clock=clock, sleep=clock.sleep, caps=CAPS) == "idle"
    clock.t += dt.timedelta(hours=3)
    rt.run_worker(conn, ex, clock=clock, sleep=clock.sleep, caps=CAPS)
    assert len(calls) == 1 and status(conn) == "WAITING_QUOTA_RESET"
    assert conn.execute("SELECT status FROM job_steps WHERE step_id='s001'").fetchone()[0] == "PENDING"
    assert "never automatically" in rt.status_report(conn, clock(), caps=CAPS)
    rt.resume(conn, jid, "operator", clock=clock)  # human confirmed the reset
    rt.run_worker(conn, lambda s: {"result_ref": "ok"}, clock=clock, caps=CAPS)
    assert status(conn) == "COMPLETED"


def test_res04_confirmed_reset_waits_until_then():
    conn, clock = connect(":memory:"), Clock()
    rt.enqueue(conn, "eval", [{}], "t", clock=clock)
    reset = clock() + dt.timedelta(hours=1)
    calls = []

    def ex(step):
        calls.append(clock())
        if len(calls) == 1:
            raise rt.QuotaExhausted(reset_at=reset)
        return {"result_ref": "ok"}

    rt.run_worker(conn, ex, clock=clock, sleep=clock.sleep, caps=CAPS)
    assert calls[1] == reset and status(conn) == "COMPLETED"


def test_res17_plan_limit_via_classifier_is_unevaluated_not_failed():
    conn, clock = connect(":memory:"), Clock()
    jid = rt.enqueue(conn, "eval", [{}], "t", clock=clock)
    calls = []

    class PlanLimit(Exception):
        pass

    def ex(step):
        calls.append(1)
        raise PlanLimit("You've hit your session limit")

    classifier = lambda e: "WAITING_QUOTA_RESET" if isinstance(e, PlanLimit) else "FAILED_PERMANENT"
    rt.run_worker(conn, ex, clock=clock, sleep=clock.sleep, caps=CAPS, classifier=classifier)
    assert len(calls) == 1 and status(conn) == "WAITING_QUOTA_RESET"  # no retry, no paid fallback path
    ev = conn.execute("SELECT payload_redacted FROM job_events WHERE job_id=? AND event_type='STATUS'"
                      " ORDER BY event_id DESC LIMIT 1", (jid,)).fetchone()[0]
    assert '"eval_status": "unevaluated"' in ev
    assert conn.execute("SELECT status FROM job_steps").fetchone()[0] == "PENDING"


def test_classifier_strings_map_to_states():
    conn, clock = connect(":memory:"), Clock()
    for state in ("FAILED_PERMANENT", "WAITING_BUDGET_APPROVAL"):
        jid = rt.enqueue(conn, "eval", [{}], "t", job_id=state, clock=clock)

        def ex(step):
            raise RuntimeError("x")

        rt.run_worker(conn, ex, clock=clock, sleep=clock.sleep, caps=CAPS, classifier=lambda e, s=state: s)
        assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (jid,)).fetchone()[0] == state
