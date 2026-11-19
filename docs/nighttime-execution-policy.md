# Nighttime execution policy (PRD 20, 25.1 step 5 - reduced)

What this is: **one** local worker process (`lab/runtime.py`) that works through an approved SQLite job
queue until 05:00 Asia/Seoul, checkpointing after every step. What it is not: a dashboard, a REST API,
multiple workers or leases (those are step 6, see "Operations (step 6)" below).

## Goal, stated honestly (PRD 25.4)

Not "never stop until 05:00", but: **keep going within the allowed time, budget and account limits; when
it has to stop, record exactly why and leave a state that can be resumed.**

What is NOT guaranteed:
- That work continues until 05:00. Plan session/weekly limits (profile S), API quotas or the budget can stop it earlier.
- That a quota-blocked job resumes the same night. A reset time is used only when it was confirmed. It is never guessed.
- Exactly-once external side effects. Steps commit idempotently in the DB (`idempotency_key`). A step that was
  killed mid-call is re-run once from the last committed checkpoint, so its external effect may happen twice
  unless the step itself is idempotent.
- Stopping a call that is already running at the exact second. 04:55-05:00 only blocks *new long* steps.
  After 05:00 no new call or retry starts. A call that is already running finishes.
- That the computer stays awake. Option A (PRD 20.7) needs the machine on.

## Window

- Timezone `Asia/Seoul`, deadline `runtime.work_until_local` (05:00). Times are stored in UTC ISO and all decisions are made in KST.
- `window_end(start)`: the next 05:00 KST after the worker starts. 23:00 Nov 19 -> 05:00 Nov 20; 01:00 Nov 20 -> 05:00 Nov 20.
- From 04:55 (`stop_scheduling_new_long_steps_before_minutes: 5`), a step with `may_run_long=1` is not started.
  The job goes to `PAUSED_WINDOW_CLOSED` and its remaining steps are kept.
- From 05:00, no new calls at all. In-progress jobs go to `PAUSED_WINDOW_CLOSED`.
- `resume_next_window: true`: `PAUSED_WINDOW_CLOSED` jobs continue in the next window from their checkpoint.
  If it is `false`, a human has to run `resume`.
- When the queue is empty the worker exits right away and jobs end as `COMPLETED`. It does not idle until 05:00.

## States (code rules, PRD 24)

| State | Auto-resume? |
|---|---|
| QUEUED / CHECKPOINTED | picked by the worker |
| RUNNING (found at worker start) | orphan of a killed process. Re-run from the last committed step |
| WAITING_RATE_LIMIT | yes, at `next_eligible_at` (Retry-After, else 2^n s backoff + jitter, cap 5 min) |
| WAITING_QUOTA_RESET | only if a confirmed reset time is stored. Otherwise never; human `resume` |
| WAITING_BUDGET_APPROVAL | never; human edits `config/budgets.yaml`, then `approve-budget` |
| WAITING_SECURITY_APPROVAL, PAUSED | never; human `resume` |
| PAUSED_WINDOW_CLOSED | next window (if `resume_next_window`) |
| COMPLETED, FAILED_PERMANENT, CANCELLED | never. A cancelled job is not re-run even after a worker restart |

- Retries are bounded: at most `retry.transient_max_attempts` (5) attempts per step. This count includes the retries Claude Code or the SDK already did internally (`attempts_used`), and attempts killed mid-step.
- A quota or plan limit gets no retry, no switch to another key and no paid fallback. The eval is recorded as **unevaluated**, not as failed.
- Every transition is written to `job_events`, with secrets redacted.
- Only approved jobs run: `enqueue` requires `approved_by`.

## Budget Guard

- Before each LLM step, `lab.budget.reserve` runs an atomic `BEGIN IMMEDIATE` reservation against the nightly scope
  (`scope_key` = the KST date the night started).
- The run-count cap (`nightly_max_agent_runs`) always applies. The USD cap applies when it is set, and then every call must
  carry an estimate, because unknown cost is never free.
- At 80% the reservation goes through and an `ALERT-01_BUDGET_SOFT_LIMIT` event is written. At 100% it is blocked and the job goes to `WAITING_BUDGET_APPROVAL`.
- The executor receives `step["caps"]` (`max_budget_usd` for `claude -p --max-budget-usd`, `max_cost_usd` for
  `claude plugin eval --max-cost-usd`) and must pass these values on.
- Code never raises a cap. Raising one means a human edits the config.
- Usage from failed attempts is settled too. If usage is unavailable, the reserved amount is kept as spend.

## Before the first night (profile S)

1. **Turn subscription usage credits off** in the Claude account settings. This is the "keep using with pay-as-you-go
   after the plan limit" option. Code cannot check it, so check it by hand and keep
   `claude.subscription_usage_credits: off` in `config/runtime.yaml` as the record.
2. Keep enough of the daytime plan usage free: set `nightly_max_agent_runs` (and the USD caps once the NO_SKILL baseline is measured).
3. `python -m lab.runtime status` should show the expected jobs as QUEUED.

## Starting the worker (OS scheduler, PRD 20.7 option A)

This repo does not register a scheduled task. Register one yourself. The `run` command waits for rate-limit
and confirmed-reset timers until 05:00. `run-once` makes a single pass with no waiting.
`--executor dry` only echoes steps. The real evaluator executor is wired in during integration.

Windows Task Scheduler (start every night at 23:00 KST):

```bat
schtasks /Create /TN "SkillLabNightly" /SC DAILY /ST 23:00 ^
  /TR "cmd /c cd /d E:\my_skills && set PYTHONIOENCODING=utf-8 && python -m lab.runtime run --executor dry >> data\nightly.log 2>&1"
```

In Task Scheduler, also set the task to "Wake the computer to run this task", and add a stop rule ("Stop the task if it runs longer than 6 hours") as a backstop.

cron (machine TZ = Asia/Seoul; otherwise use `CRON_TZ=Asia/Seoul`):

```cron
CRON_TZ=Asia/Seoul
0 23 * * * cd /path/to/my_skills && PYTHONIOENCODING=utf-8 python -m lab.runtime run --executor dry >> data/nightly.log 2>&1
```

Do not use `/loop`, chat sessions or cloud scheduled tasks for the nightly queue (PRD 20.1, 20.7).

## Stopping a running `claude -p`

If a `claude -p` call has to be cut near 05:00, send **SIGINT first** (Ctrl+C, or `CTRL_C_EVENT` on Windows) so
the current turn can finish. Use SIGTERM or kill only after a grace period. Exit code 143 (SIGTERM) means the turn
left no result, and the step re-runs from the last committed checkpoint.

## Operations (step 6)

- **Leases (RES-09).** A worker only works on a job while it holds the job's lease (`jobs.lease_owner`,
  `jobs.lease_until`, TTL 600 s). The lease is taken atomically (`BEGIN IMMEDIATE`), renewed before every step
  and released when the worker stops. A `RUNNING` job is recovered as an orphan only if its lease is free,
  expired or the worker's own. `--worker-id` defaults to `hostname:pid`. Several workers may share the DB, but
  start at most `runtime.max_concurrent_workers` of them yourself; the runtime never spawns workers. The lease is
  renewed only between steps, so no step may run longer than the TTL.
- **Alerts (PRD 21.2)** are rows in `alerts`, deduplicated by fingerprint with a 1 h cooldown (repeats count in
  `occurrences`): ALERT-01 soft budget, ALERT-02 `WAITING_BUDGET_APPROVAL`, ALERT-03 `WAITING_QUOTA_RESET`,
  ALERT-04 orphan recovery or `FAILED_PERMANENT`, ALERT-05 `PAUSED_WINDOW_CLOSED`, ALERT-06 a call using more than
  3x the median tokens of its step (`check_token_spikes`, run by `runtime status`). Delivery means
  `python -m lab.runtime status` prints undelivered alerts and marks them delivered. Nothing is sent outside the
  machine; that would need user approval.
- **Dashboard:** `python -m lab dashboard --out reports/dashboard.html` writes a static HTML snapshot.
- **API:** `python -m lab api --port 8765` binds 127.0.0.1. GET endpoints are open locally. POST
  (`/jobs`, `/jobs/{id}/pause|resume|cancel|approve-budget`) is disabled unless `LAB_API_TOKEN` is set, and then
  needs `Authorization: Bearer <token>`. Each action is logged in `job_events` with `by = api:<name>`.
