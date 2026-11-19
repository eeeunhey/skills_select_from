"""RES-01 (kill mid-job -> resume from last committed step, no duplicates), RES-02-style idempotent commit,
RES-15 (cancel then restart -> not re-run)."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab import runtime as rt  # noqa: E402
from lab.db import connect  # noqa: E402

CAPS = {"nightly_max_usd": None, "nightly_max_agent_runs": 30, "per_call_max_usd": None,
        "per_suite_max_usd": None, "soft_limit_ratio": 0.8, "hard_limit_ratio": 1.0}
T = dt.datetime(2026, 11, 19, 23, 0, tzinfo=rt.KST)


class Killed(BaseException):
    """Stands in for the process dying (not an Exception, so the worker can't handle it)."""


def test_res01_kill_mid_job_resumes_from_last_committed_step(tmp_path):
    db = tmp_path / "lab.db"
    conn = connect(db)
    jid = rt.enqueue(conn, "collect", [{"needs_llm": True}] * 5, "tester", clock=lambda: T)
    calls = []

    def dies_on_3(step):
        calls.append(step["step_id"])
        if step["step_id"] == "s003":
            raise Killed()
        return {"result_ref": "r:" + step["step_id"], "usd": 0.0}

    with pytest.raises(Killed):
        rt.run_worker(conn, dies_on_3, clock=lambda: T, caps=CAPS)
    conn.close()
    conn = connect(db)  # restarted process
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "RUNNING"
    rt.run_worker(conn, lambda s: calls.append(s["step_id"]) or {"result_ref": "r:" + s["step_id"]},
                  clock=lambda: T, caps=CAPS)
    assert calls == ["s001", "s002", "s003", "s003", "s004", "s005"]  # s003 re-run once, s001/s002 not
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "COMPLETED"
    assert tuple(conn.execute("SELECT COUNT(*), COUNT(DISTINCT step_id) FROM checkpoints WHERE job_id=?",
                        (jid,)).fetchone()) == (5, 5)
    assert conn.execute("SELECT attempt_count FROM job_steps WHERE step_id='s003'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM job_events WHERE event_type='RECOVERED_ORPHAN'").fetchone()[0] == 1


def test_commit_is_idempotent():
    conn = connect(":memory:")
    jid = rt.enqueue(conn, "x", [{}], "t", clock=lambda: T)
    key = conn.execute("SELECT idempotency_key FROM job_steps").fetchone()[0]
    assert rt.commit_step(conn, jid, "s001", key, "ref", T) is True
    assert rt.commit_step(conn, jid, "s001", key, "ref", T) is False
    assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1


def test_crash_loop_is_bounded(tmp_path):
    db = tmp_path / "lab.db"
    rt.enqueue(connect(db), "x", [{"needs_llm": False}], "t", clock=lambda: T)

    def die(_):
        raise Killed()

    for _ in range(5):
        with pytest.raises(Killed):
            rt.run_worker(connect(db), die, clock=lambda: T, caps=CAPS)
    calls = []
    rt.run_worker(connect(db), lambda s: calls.append(1), clock=lambda: T, caps=CAPS)
    assert calls == [] and connect(db).execute("SELECT status FROM jobs").fetchone()[0] == "FAILED_PERMANENT"


def test_res15_cancel_then_restart_not_rerun(tmp_path):
    db = tmp_path / "lab.db"
    conn = connect(db)
    jid = rt.enqueue(conn, "x", [{"needs_llm": False}] * 3, "t", clock=lambda: T)
    calls = []

    def cancel_after_first(step):
        calls.append(step["step_id"])
        rt.cancel(conn, jid, "operator", clock=lambda: T)  # operator cancels while step 1 runs
        return {"result_ref": "r"}

    rt.run_worker(conn, cancel_after_first, clock=lambda: T, caps=CAPS)
    conn.close()
    conn = connect(db)
    rt.run_worker(conn, lambda s: calls.append(s["step_id"]), clock=lambda: T, caps=CAPS)
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "CANCELLED"
    assert calls == ["s001"]
    with pytest.raises(ValueError):
        rt.resume(conn, jid, "someone", clock=lambda: T)


def test_unapproved_job_refused():
    with pytest.raises(ValueError):
        rt.enqueue(connect(":memory:"), "x", [{}], approved_by=None)


def test_events_are_redacted():
    conn = connect(":memory:")
    jid = rt.enqueue(conn, "x", [{"needs_llm": False}], "t", clock=lambda: T)

    def boom(_):
        raise rt.PermanentError(msg="401 bad key sk-ant-api03-SECRETSECRET")

    rt.run_worker(conn, boom, clock=lambda: T, caps=CAPS)
    blob = " ".join(r[0] for r in conn.execute("SELECT payload_redacted FROM job_events WHERE job_id=?", (jid,)))
    assert "SECRETSECRET" not in blob and "[REDACTED]" in blob
