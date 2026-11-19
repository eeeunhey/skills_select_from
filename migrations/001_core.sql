-- PRD 5.3 core tables (+ v1.3 columns from 22.1). Versioned rows are append-only.
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,                -- e.g. github:anthropics/skills
  url TEXT NOT NULL,
  type TEXT NOT NULL,                 -- github_repo | manual
  priority INTEGER NOT NULL DEFAULT 1,
  claude_native INTEGER NOT NULL DEFAULT 0,  -- 1 = written for Claude (5.1 v1.3); else compatibility unverified
  enabled INTEGER NOT NULL DEFAULT 1,
  scan_cursor TEXT,                   -- last commit sha scanned
  last_success_at TEXT
);

CREATE TABLE IF NOT EXISTS skills (
  id TEXT PRIMARY KEY,                -- github:owner/repo:path/SKILL.md
  source_id TEXT NOT NULL REFERENCES sources(id),
  repo TEXT NOT NULL,
  path TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  distribution TEXT NOT NULL,         -- standalone | plugin
  plugin_name TEXT,
  status TEXT NOT NULL DEFAULT 'active',            -- active | removed | moved
  moved_to TEXT,
  duplicate_of TEXT,                  -- skill id with identical content_hash (first seen wins)
  review_status TEXT NOT NULL DEFAULT 'discovered', -- discovered|analyzed|selected|experimented|verified (7.1)
  security_status TEXT NOT NULL DEFAULT 'unreviewed', -- unreviewed|flagged|quarantined|approved
  compatibility_status TEXT NOT NULL DEFAULT 'unverified', -- unverified|claude_verified
  license TEXT,
  current_version_id INTEGER,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  last_changed TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_id TEXT NOT NULL REFERENCES skills(id),
  commit_sha TEXT NOT NULL,
  blob_sha TEXT NOT NULL,
  content_hash TEXT NOT NULL,         -- sha256 of SKILL.md
  fetched_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL,        -- frontmatter + catalog fields (F2 v1.3)
  scan_json TEXT NOT NULL,            -- static security scan result
  analysis_version TEXT NOT NULL,
  UNIQUE(skill_id, blob_sha)
);
CREATE TRIGGER IF NOT EXISTS skill_versions_no_update BEFORE UPDATE ON skill_versions
BEGIN SELECT RAISE(ABORT, 'skill_versions are append-only'); END;

CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
  skill_id UNINDEXED, name, description, path, hints, tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS collection_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  counts_json TEXT,
  errors_json TEXT,
  github_api_calls INTEGER NOT NULL DEFAULT 0   -- OBS-12: infra usage, never LLM tokens
);

CREATE TABLE IF NOT EXISTS rubrics (
  id TEXT NOT NULL,                   -- case_id, e.g. T01
  version TEXT NOT NULL,              -- rubric_version, e.g. rubric-v1
  yaml_text TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  author TEXT NOT NULL,
  created_at TEXT NOT NULL,
  approved_by TEXT,
  approved_at TEXT,
  PRIMARY KEY (id, version)
);
CREATE TRIGGER IF NOT EXISTS rubrics_frozen BEFORE UPDATE ON rubrics WHEN OLD.approved_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'approved rubric is frozen; create a new version'); END;
CREATE TRIGGER IF NOT EXISTS rubrics_no_delete BEFORE DELETE ON rubrics WHEN OLD.approved_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'approved rubric cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,                -- case_id
  prompt TEXT NOT NULL,
  context_json TEXT NOT NULL DEFAULT '{}',   -- task_type, stack, constraints...
  environment_json TEXT NOT NULL DEFAULT '{}',
  fixture_commit TEXT,
  rubric_id TEXT,
  rubric_version TEXT,
  split TEXT NOT NULL DEFAULT 'labeled'      -- labeled | holdout
);

CREATE TABLE IF NOT EXISTS recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id TEXT NOT NULL,
  task_id TEXT,
  query TEXT NOT NULL,
  skill_id TEXT,                      -- NULL = NO_SKILL
  rank INTEGER NOT NULL,
  score_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  unknowns_json TEXT NOT NULL,
  decision TEXT NOT NULL DEFAULT 'PROPOSED_NOT_VALIDATED',
  policy_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  approved_by TEXT,
  approved_at TEXT
);

CREATE TABLE IF NOT EXISTS eval_runs (
  run_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  rubric_version TEXT NOT NULL,
  arm TEXT NOT NULL CHECK (arm IN ('NO_SKILL','AUTO','FORCED')),
  candidate_role TEXT,                -- B (top-1) | C (alternative); NULL for NO_SKILL
  skill_id TEXT,
  skill_version_id INTEGER,
  model_id TEXT NOT NULL,
  judge_model_id TEXT,
  claude_code_version TEXT NOT NULL,
  auth_profile TEXT NOT NULL CHECK (auth_profile IN ('S','K')),
  engine TEXT NOT NULL,               -- plugin_eval | claude_p | manual
  run_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,               -- PASSED|FAILED|UNEVALUATED|BLOCKED|QUARANTINED|INVALID
  success INTEGER,                    -- NULL when not evaluated
  checks_passed INTEGER,
  checks_total INTEGER,
  trigger_observed INTEGER,           -- Skill tool_use seen in transcript (NULL = unknown)
  isolation_ok INTEGER,               -- 0 => personal ~/.claude leaked (RES-18) => INVALID
  cost_usd REAL,
  latency_ms INTEGER,
  num_turns INTEGER,
  session_id TEXT,
  artifact_uri TEXT,
  started_at TEXT,
  finished_at TEXT,
  notes TEXT
);
CREATE TRIGGER IF NOT EXISTS eval_runs_no_delete BEFORE DELETE ON eval_runs
BEGIN SELECT RAISE(ABORT, 'failed or not, eval runs are never deleted'); END;

CREATE TABLE IF NOT EXISTS eval_checks (
  run_id TEXT NOT NULL REFERENCES eval_runs(run_id),
  requirement_id TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('pass','fail','unevaluated')),
  verifier TEXT NOT NULL,             -- external_test | log_policy | builtin_grader | human
  evidence_uri TEXT,
  PRIMARY KEY (run_id, requirement_id, verifier)
);

CREATE TABLE IF NOT EXISTS failure_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT,
  run_id TEXT,
  decision_id TEXT,
  category TEXT NOT NULL,             -- RETRIEVAL|RANKING|OVERCONFIDENT|ENVIRONMENT|EXECUTION|TRIGGER|COST|RUBRIC
  explanation TEXT NOT NULL,
  evidence_uri TEXT,
  proposed_fix TEXT,
  origin TEXT NOT NULL DEFAULT 'auto', -- auto (suggested) | human
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
