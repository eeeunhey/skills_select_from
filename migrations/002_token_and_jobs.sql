-- PRD 19 (token ledger), 20 (jobs/checkpoints, reduced step-5 scope), 22.1 (+ v1.3 Claude columns).
CREATE TABLE IF NOT EXISTS llm_calls (
  call_id TEXT PRIMARY KEY,
  timestamp_utc TEXT NOT NULL,
  run_id TEXT,                        -- eval run or job run
  step_id TEXT,
  attempt_id INTEGER NOT NULL DEFAULT 1,
  stage TEXT NOT NULL,                -- collect|search|rerank|analyze|execute|evaluate|report
  agent_role TEXT NOT NULL,
  action TEXT,
  skill_id TEXT NOT NULL DEFAULT 'NO_SKILL',
  skill_hash TEXT,
  project_id TEXT,
  provider TEXT NOT NULL DEFAULT 'anthropic',
  auth_profile TEXT,                  -- S | K
  call_path TEXT NOT NULL,            -- claude_cli | messages_api
  model_id TEXT,
  request_id TEXT,
  session_id TEXT,
  claude_code_version TEXT,
  arm TEXT,
  skill_tool_called INTEGER,
  num_turns INTEGER,
  usage_source TEXT NOT NULL CHECK (usage_source IN ('provider','estimated','unavailable')),
  input_tokens INTEGER,               -- uncached remainder only; NULL when unavailable (never 0-filled)
  cache_write_tokens INTEGER,
  cached_input_tokens INTEGER,
  output_tokens INTEGER,
  reasoning_tokens INTEGER,
  provider_usage_json TEXT,           -- redacted raw usage block
  prompt_parts_estimate_json TEXT,    -- estimate, never presented as measured
  latency_ms INTEGER,
  status TEXT NOT NULL,
  error_code TEXT,
  estimated_cost REAL,                -- client-side estimate (claude total_cost_usd) - not billing
  price_version TEXT,
  currency TEXT NOT NULL DEFAULT 'USD',
  cache_hit INTEGER NOT NULL DEFAULT 0,
  reused_artifact INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT UNIQUE,
  checkpoint_id TEXT
);
CREATE TRIGGER IF NOT EXISTS llm_calls_no_delete BEFORE DELETE ON llm_calls
BEGIN SELECT RAISE(ABORT, 'ledger rows are never deleted'); END;

CREATE TABLE IF NOT EXISTS price_schedules (
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  price_type TEXT NOT NULL,           -- input|output|cache_write|cache_read (USD per 1M tokens)
  rate REAL NOT NULL,
  price_version TEXT NOT NULL,
  PRIMARY KEY (provider, model_id, valid_from, price_type)
);

CREATE TABLE IF NOT EXISTS token_reservations (
  reservation_id TEXT PRIMARY KEY,
  job_id TEXT,
  scope TEXT NOT NULL,                -- per_call|per_suite|nightly_window|daily|monthly|evaluation_campaign
  scope_key TEXT NOT NULL,            -- e.g. nightly window date '2026-11-19'
  reserved_usd REAL NOT NULL DEFAULT 0,
  reserved_runs INTEGER NOT NULL DEFAULT 0,
  settled_usd REAL,
  status TEXT NOT NULL CHECK (status IN ('reserved','settled','released')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  run_id TEXT,
  kind TEXT NOT NULL,                 -- e.g. eval_experiment
  status TEXT NOT NULL,               -- QUEUED|RUNNING|CHECKPOINTED|COMPLETED|WAITING_*|PAUSED_WINDOW_CLOSED|FAILED_PERMANENT|CANCELLED
  window_end_at TEXT,                 -- UTC ISO of 05:00 KST deadline
  next_eligible_at TEXT,
  owner_id TEXT,
  approved_by TEXT,                   -- jobs only run inside an approved scope (24)
  workflow_version TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_steps (
  step_id TEXT NOT NULL,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  seq INTEGER NOT NULL,
  status TEXT NOT NULL,               -- PENDING|DONE|FAILED
  attempt_count INTEGER NOT NULL DEFAULT 0,
  may_run_long INTEGER NOT NULL DEFAULT 1,
  needs_llm INTEGER NOT NULL DEFAULT 1,
  estimated_usd REAL,
  idempotency_key TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  result_ref TEXT,
  PRIMARY KEY (job_id, step_id),
  UNIQUE (job_id, step_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  step_id TEXT,
  cursor TEXT,
  artifact_ref TEXT,
  state_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_redacted TEXT,
  created_at TEXT NOT NULL
);
