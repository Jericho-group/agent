-- ─────────────────────────────────────────────────────────────────
-- PRM v1.1 — новые таблицы (CREATE, idempotent)
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS integrations (
  id                    serial PRIMARY KEY,
  name                  text NOT NULL,
  external_id           text UNIQUE,
  api_credential_hash   text NOT NULL,          -- bcrypt
  embed_sign_secret_enc bytea NOT NULL,         -- AES-256-GCM (либо raw 32 байта для MVP)
  webhook_secret_enc    bytea,                  -- AES-256-GCM, NULL если polling
  webhook_url           text,
  allowed_origins       text[] NOT NULL DEFAULT '{}',
  status                text NOT NULL DEFAULT 'active',
  created_at            timestamptz DEFAULT now(),
  updated_at            timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenant_operators (
  id           serial PRIMARY KEY,
  tenant_id    int NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  external_id  text NOT NULL,
  email        text,
  name         text,
  role         text NOT NULL DEFAULT 'admin',
  perms        jsonb DEFAULT '{}',
  active       bool NOT NULL DEFAULT true,
  created_at   timestamptz DEFAULT now(),
  UNIQUE (tenant_id, external_id)
);

CREATE TABLE IF NOT EXISTS tenant_contacts (
  id           serial PRIMARY KEY,
  tenant_id    int NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  external_id  text NOT NULL,
  email        text,
  name         text,
  active       bool NOT NULL DEFAULT true,
  created_at   timestamptz DEFAULT now(),
  UNIQUE (tenant_id, external_id)
);

CREATE TABLE IF NOT EXISTS embed_sessions (
  code                text PRIMARY KEY,        -- 32 байта hex, one-time
  tenant_id           int NOT NULL,
  actor_id            int NOT NULL,
  actor_type          text NOT NULL,           -- 'operator' | 'contact'
  perms               jsonb,
  created_at          timestamptz DEFAULT now(),
  expires_at          timestamptz NOT NULL,    -- created + 60s
  used_at             timestamptz,
  session_id          text,                    -- 24 байта hex, присваивается на /embed
  session_expires_at  timestamptz,             -- max 8ч
  last_activity_at    timestamptz,             -- для idle 30 мин
  revoked_at          timestamptz
);
CREATE INDEX IF NOT EXISTS ix_embed_sessions_actor
  ON embed_sessions(tenant_id, actor_id, actor_type);
CREATE INDEX IF NOT EXISTS ix_embed_sessions_session
  ON embed_sessions(session_id) WHERE session_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS integration_events (
  id             bigserial PRIMARY KEY,
  integration_id int NOT NULL REFERENCES integrations(id),
  tenant_id      int,
  actor_id       int,
  actor_type     text,
  event_type     text NOT NULL,
  payload        jsonb,
  created_at     timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_integration_events_cursor
  ON integration_events(integration_id, id);

CREATE TABLE IF NOT EXISTS integration_audit (
  id             bigserial PRIMARY KEY,
  integration_id int NOT NULL,
  actor_ip       inet,
  method         text,
  path           text,
  status_code    int,
  payload_masked jsonb,
  ts             timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS async_jobs (
  id             text PRIMARY KEY,             -- UUID / job_<hex>
  integration_id int NOT NULL,
  tenant_id      int,
  job_type       text NOT NULL,                -- 'export', ...
  status         text NOT NULL DEFAULT 'queued',
  result         jsonb,
  created_at     timestamptz DEFAULT now(),
  updated_at     timestamptz DEFAULT now()
);
