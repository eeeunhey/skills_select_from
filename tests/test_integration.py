"""runtime <-> evaluator wiring: evaluator states map to job states (no real Claude)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab import db, evaluator, runtime  # noqa: E402


def _planned(conn, exp, n):
    for i in range(n):
        conn.execute("INSERT INTO eval_runs (run_id, experiment_id, task_id, rubric_version, arm, model_id,"
                     " claude_code_version, auth_profile, engine, status) VALUES (?,?, 'T01', 'rubric-v1', 'NO_SKILL',"
                     " 'claude-haiku-5-5', '2.1.327', 'S', 'claude_p', 'PLANNED')", (f"{exp}-{i}", exp))


def test_eval_executor_maps_states(monkeypatch):
    conn = db.connect(":memory:")
    _planned(conn, "exp-1", 3)
    states = iter(["COMPLETED", "WAITING_QUOTA_RESET"])
    calls = []
    monkeypatch.setattr(evaluator, "execute", lambda c, exp, limit=None: calls.append(exp) or next(states))
    job = runtime.enqueue_experiment(conn, "exp-1", approved_by="operator")
    runtime.run_worker(conn, runtime.eval_executor(conn), sleep=None, max_iterations=10)
    st = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job,)).fetchone()[0]
    done = conn.execute("SELECT count(*) FROM job_steps WHERE job_id=? AND status='DONE'", (job,)).fetchone()[0]
    assert st == "WAITING_QUOTA_RESET" and done == 1 and calls == ["exp-1", "exp-1"]   # stops at the quota wall


def test_enqueue_requires_planned_runs():
    conn = db.connect(":memory:")
    try:
        runtime.enqueue_experiment(conn, "nope", approved_by="operator")
        raise AssertionError("should refuse")
    except ValueError:
        pass


def test_execute_refuses_when_fixture_changed_after_registration():
    """fixture/verifier are ground truth: a change after planning must block execution (new rubric_version)."""
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO tasks (id, prompt, fixture_commit, rubric_id, rubric_version) VALUES"
                 " ('T01', 'p', 'sha256:stale', 'T01', 'rubric-v1')")
    _planned(conn, "exp-f", 1)
    try:
        evaluator.execute(conn, "exp-f", dry_run=True, log=lambda *a: None)
        raise AssertionError("should refuse")
    except evaluator.Refused as e:
        assert "fixture" in str(e)
