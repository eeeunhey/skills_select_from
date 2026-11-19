"""PRD 20.2 window + RES-06 (04:57 long step) + RES-07 (next night resumes from checkpoint)."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab import runtime as rt  # noqa: E402
from lab.db import connect  # noqa: E402

KST = rt.KST
CAPS = {"nightly_max_usd": None, "nightly_max_agent_runs": 30, "per_call_max_usd": None,
        "per_suite_max_usd": None, "soft_limit_ratio": 0.8, "hard_limit_ratio": 1.0}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += dt.timedelta(seconds=s)


def kst(*a):
    return dt.datetime(*a, tzinfo=KST)


def test_window_end_examples_from_prd_20_2():
    assert rt.window_end(kst(2026, 11, 19, 23, 0)) == kst(2026, 11, 20, 5, 0)
    assert rt.window_end(kst(2026, 11, 20, 1, 0)) == kst(2026, 11, 20, 5, 0)
    assert rt.window_end(kst(2026, 11, 20, 5, 0)) == kst(2026, 11, 21, 5, 0)
    # UTC input, KST decision: 2026-11-19 14:00Z == 23:00 KST
    utc = dt.datetime(2026, 11, 19, 14, 0, tzinfo=dt.timezone.utc)
    assert rt.window_end(utc) == kst(2026, 11, 20, 5, 0)
    assert rt.iso(rt.window_end(utc)) == "2026-11-19T20:00:00+00:00"
    assert rt.night_key(rt.window_end(utc)) == "2026-11-19"


def test_near_end():
    end = kst(2026, 11, 20, 5, 0)
    assert not rt.near_end(kst(2026, 11, 20, 4, 54), end)
    assert rt.near_end(kst(2026, 11, 20, 4, 55), end)
    assert rt.near_end(kst(2026, 11, 20, 4, 57), end)


def test_res06_long_step_at_0457_not_started_and_preserved():
    conn = connect(":memory:")
    clock = Clock(kst(2026, 11, 20, 4, 57))
    jid = rt.enqueue(conn, "eval", [{"may_run_long": True}, {"may_run_long": True}], "tester", clock=clock)
    calls = []
    res = rt.run_worker(conn, lambda s: calls.append(s["step_id"]), clock=clock, sleep=clock.sleep, caps=CAPS)
    assert calls == []
    assert res == "idle"
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "PAUSED_WINDOW_CLOSED"
    assert [r[0] for r in conn.execute("SELECT status FROM job_steps WHERE job_id=?", (jid,))] == ["PENDING", "PENDING"]


def test_short_step_allowed_before_0500_but_nothing_after():
    conn = connect(":memory:")
    clock = Clock(kst(2026, 11, 20, 4, 57))
    end = rt.window_end(clock())
    rt.enqueue(conn, "eval", [{"may_run_long": False, "needs_llm": False}] * 1, "t", job_id="a", clock=clock)
    calls = []
    rt.run_worker(conn, lambda s: calls.append(s["step_id"]), clock=clock, caps=CAPS)
    assert calls == ["s001"]
    clock.t = kst(2026, 11, 20, 5, 1)
    rt.enqueue(conn, "eval", [{"may_run_long": False, "needs_llm": False}], "t", job_id="b", clock=clock)
    assert rt.run_worker(conn, lambda s: calls.append(s), clock=clock, caps=CAPS, window_end_at=end) == "window_closed"
    assert calls == ["s001"]


def test_res07_next_night_resumes_from_checkpoint_without_rerun():
    conn = connect(":memory:")
    clock = Clock(kst(2026, 11, 19, 23, 0))
    jid = rt.enqueue(conn, "eval", [{"needs_llm": False}] * 5, "tester", clock=clock)
    calls = []

    def ex(step):  # each step takes 2h
        calls.append(step["step_id"])
        clock.t += dt.timedelta(hours=2)
        return {"result_ref": "r:" + step["step_id"]}

    assert rt.run_worker(conn, ex, clock=clock, sleep=clock.sleep, caps=CAPS) == "window_closed"
    assert calls == ["s001", "s002", "s003"]  # 23->01->03->05
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "PAUSED_WINDOW_CLOSED"
    assert "window ends" in rt.status_report(conn, clock(), caps=CAPS)

    clock.t = kst(2026, 11, 20, 23, 0)  # next night
    assert rt.run_worker(conn, ex, clock=clock, sleep=clock.sleep, caps=CAPS) == "idle"
    assert calls == ["s001", "s002", "s003", "s004", "s005"]
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "COMPLETED"
    assert conn.execute("SELECT COUNT(*) FROM checkpoints WHERE job_id=?", (jid,)).fetchone()[0] == 5


def test_paused_window_closed_needs_approval_when_resume_next_window_off():
    conn = connect(":memory:")
    clock = Clock(kst(2026, 11, 20, 4, 57))
    jid = rt.enqueue(conn, "eval", [{"may_run_long": True, "needs_llm": False}], "t", clock=clock)
    cfg = rt.load_runtime()
    cfg["runtime"]["resume_next_window"] = False
    rt.run_worker(conn, lambda s: None, clock=clock, caps=CAPS, cfg=cfg)
    clock.t = kst(2026, 11, 20, 23, 0)
    calls = []
    rt.run_worker(conn, lambda s: calls.append(1), clock=clock, caps=CAPS, cfg=cfg)
    assert calls == []
    rt.resume(conn, jid, "operator", clock=clock)
    rt.run_worker(conn, lambda s: calls.append(1), clock=clock, caps=CAPS, cfg=cfg)
    assert calls == [1]
