CREATE TABLE IF NOT EXISTS worker_runs (
  id          bigserial PRIMARY KEY,
  worker      text NOT NULL,           -- 'retention' | 'email' | 'webhook'
  started_at  timestamptz DEFAULT now(),
  finished_at timestamptz,
  ok          bool,
  result      jsonb,                    -- {events_deleted, sessions_deleted, ...} etc
  error       text
);
CREATE INDEX IF NOT EXISTS ix_worker_runs_recent ON worker_runs(worker, started_at DESC);
