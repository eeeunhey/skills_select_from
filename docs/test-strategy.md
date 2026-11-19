# Test strategy (PRD 18.2, 23)

| Level | What it proves | Where |
|---|---|---|
| 1. Static | format, permissions, license, duplicates. Says **nothing** about performance | `tests/test_collector.py`, `tests/test_security_gates.py` |
| 2. Search / selection | predictions only: candidates and reasons | `tests/test_selector.py`, `python -m lab.audit` (labeled + holdout sets) |
| 3. Isolated execution | same task with `NO_SKILL` / `AUTO` / `FORCED` | `python -m lab.evaluator` |
| 4. Independent outcome | frozen rubric + external verifier on the produced workdir | `fixtures/react-demo/verify/` |
| 5. Usability | can a beginner accept or reject a recommendation correctly | manual. Record in `reports/user-testing/` |
| 6. Regression | re-run affected cases after a skill, model or policy change | `python -m lab.audit --compare` |

Offline unit tests never call Claude or GitHub. They use fakes, so `python -m pytest -q` is free and deterministic.

## Resilience tests (23)
| RES | Test |
|---|---|
| 01, 07 | `tests/test_checkpoint_resume.py` |
| 03 | `tests/test_retry_policy.py` |
| 04, 17 | `tests/test_retry_policy.py`, `tests/test_evaluation.py` |
| 06 | `tests/test_nightly_window_5am.py` |
| 08 | `tests/test_budget_limits.py` |
| 10, 11, 13, 18, 19 | `tests/test_usage_accounting.py`, `tests/test_evaluation.py` |
| 15 | `tests/test_checkpoint_resume.py` |
| 16 | `tests/test_security_gates.py` |
| 09 | `tests/test_ops.py` (two workers on one DB file: every step runs exactly once; an expired lease is taken over, a live one is not) |

Step 6 ops (alerts ALERT-01..06 dedup/cooldown, dashboard, REST API token gate) are also in `tests/test_ops.py`.
