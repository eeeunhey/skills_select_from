# Recovery runbook (PRD 20.5)

First run `python -m lab.runtime status` (or with `--db PATH`). Every job that is not completed shows its state, steps done,
last checkpoint, next eligible time, **why** it stopped and **when/how** it resumes.

Operator commands (each is recorded in `job_events` with `--by`):

```
python -m lab.runtime pause JOB --by NAME
python -m lab.runtime resume JOB --by NAME          # any paused/waiting job except CANCELLED/COMPLETED/FAILED_PERMANENT
python -m lab.runtime approve-budget JOB --by NAME  # only WAITING_BUDGET_APPROVAL; does NOT raise caps
python -m lab.runtime cancel JOB --by NAME
```

| Failure | What status shows | What to do |
|---|---|---|
| 429 with Retry-After / 5xx / overloaded | `WAITING_RATE_LIMIT`, "retry in Ns", next eligible time | Nothing. It resumes on its own. After 5 attempts in total (CLI internal retries included) it goes to `FAILED_PERMANENT`. |
| API quota / credits / 402 / 429 without Retry-After | `WAITING_QUOTA_RESET`, "reset time unknown - waiting for operator" | Check the provider console. Fix billing or wait for the reset, then `resume`. Never switch keys. |
| Plan session/weekly limit (profile S) | `WAITING_QUOTA_RESET`, eval marked `unevaluated` | Check the reset time in the Claude app. If it is confirmed, the classifier can store it and the job resumes then. Otherwise `resume` after the reset. Make sure usage credits are **off**. |
| Project budget cap (our cap, or HTTP 400 org spend limit, or `error_max_budget_usd`) | `WAITING_BUDGET_APPROVAL`, "hard limit nightly_max_usd: ..." | Decide. To spend more, edit `config/budgets.yaml` by hand, then `approve-budget`. If the cap is still exceeded it blocks again. Or wait for the next night's budget key and `approve-budget`. |
| Soft limit (80%) | event `ALERT-01_BUDGET_SOFT_LIMIT`, job keeps running | Optional: pause non-essential jobs. |
| 401 / 403 / unknown error | `FAILED_PERMANENT`, reason | Fix the credentials or the cause, then enqueue a new job. Completed steps of the old job stay in `checkpoints`. |
| Worker/PC killed mid-step | job still `RUNNING` with no process alive; `RECOVERED_ORPHAN` event on next start | Start the worker again (or let the scheduler do it). The killed step re-runs once. Committed steps are not redone. If a step is killed 5 times, it goes to `FAILED_PERMANENT`. |
| `claude -p` exit 143 (SIGTERM) | same as a kill: step re-runs | Nothing. Next time send SIGINT before SIGTERM. |
| Long step near 05:00 / window closed | `PAUSED_WINDOW_CLOSED`, "long step not started within 5 min of 05:00 KST" or "05:00 KST window closed" | Nothing. It continues next night. If `resume_next_window: false`, run `resume`. |
| Security approval pending | `WAITING_SECURITY_APPROVAL` | Review the candidate (PRD 12/RES-16), then `resume`. It is never resumed automatically. |
| Operator cancelled | `CANCELLED` | Final. A worker restart does not re-run it. Enqueue a new job if needed. |

Notes:
- Reservations left `reserved` by a killed process count as spend (conservative). Only release them by hand
  after checking `llm_calls` for that step.
- To check the spend of any step: look at `token_reservations` (settled_usd) and `llm_calls` (usage_source; `unavailable` means
  unknown, not 0).
