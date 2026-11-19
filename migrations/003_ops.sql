-- PRD 21.2 alerts (dedup by fingerprint + cooldown), RES-09 job leases (25.1 step 6).
CREATE TABLE IF NOT EXISTS alerts (
  alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT,
  kind TEXT NOT NULL,                 -- ALERT-01..06 (PRD 21.2)
  fingerprint TEXT NOT NULL,
  message TEXT NOT NULL,
  occurrences INTEGER NOT NULL DEFAULT 1,  -- repeats suppressed by cooldown are counted, not re-sent
  created_at TEXT NOT NULL,           -- last (re)raise outside cooldown
  delivered_at TEXT,                  -- NULL = not yet shown to an operator
  UNIQUE (fingerprint)
);
ALTER TABLE jobs ADD COLUMN lease_owner TEXT;
ALTER TABLE jobs ADD COLUMN lease_until TEXT;
