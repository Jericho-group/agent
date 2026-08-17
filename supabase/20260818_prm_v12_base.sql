-- ─────────────────────────────────────────────────────────────────
-- PRM v1.2 base migration: v1.1 tables + tenants columns + leads assignment
-- Идемпотентно. Заря (tenant_id=6, parent_integration_id IS NULL) НЕ задета.
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS integrations (
  id                    serial PRIMARY KEY,
  name                  text NOT NULL,
  external_id           text UNIQUE,
  api_credential_hash   text NOT NULL,
  embed_sign_secret_enc bytea NOT NULL,
  webhook_secret_enc    bytea,
  webhook_url           text,
  allowed_origins       text[] NOT NULL DEFAULT '{}',
  status                text NOT NULL DEFAULT 'active',
  created_at            timestamptz DEFAULT now(),
  updated_at            timestamptz DEFAULT now()
);

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS parent_integration_id int REFERENCES integrations(id);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS external_id text;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS delete_scheduled_at timestamptz;
CREATE UNIQUE INDEX IF NOT EXISTS ux_tenants_ext
  ON tenants(parent_integration_id, external_id)
  WHERE parent_integration_id IS NOT NULL AND external_id IS NOT NULL;

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
  id                serial PRIMARY KEY,
  tenant_id         int NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  external_id       text NOT NULL,
  email             text,
  name              text,
  active            bool NOT NULL DEFAULT true,
  parent_contact_id int REFERENCES tenant_contacts(id),   -- Task 4: sub-partners v2
  created_at        timestamptz DEFAULT now(),
  UNIQUE (tenant_id, external_id)
);
CREATE INDEX IF NOT EXISTS ix_contacts_parent ON tenant_contacts(parent_contact_id)
  WHERE parent_contact_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS embed_sessions (
  code                text PRIMARY KEY,
  tenant_id           int NOT NULL,
  actor_id            int NOT NULL,
  actor_type          text NOT NULL,           -- 'operator' | 'contact' | 'super_admin'
  perms               jsonb,
  created_at          timestamptz DEFAULT now(),
  expires_at          timestamptz NOT NULL,
  used_at             timestamptz,
  session_id          text,
  session_expires_at  timestamptz,
  last_activity_at    timestamptz,
  revoked_at          timestamptz
);
CREATE INDEX IF NOT EXISTS ix_embed_sessions_actor ON embed_sessions(tenant_id, actor_id, actor_type);
CREATE INDEX IF NOT EXISTS ix_embed_sessions_session ON embed_sessions(session_id) WHERE session_id IS NOT NULL;

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
CREATE INDEX IF NOT EXISTS ix_integration_events_cursor ON integration_events(integration_id, id);

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
  id             text PRIMARY KEY,
  integration_id int NOT NULL,
  tenant_id      int,
  job_type       text NOT NULL,
  status         text NOT NULL DEFAULT 'queued',
  result         jsonb,
  created_at     timestamptz DEFAULT now(),
  updated_at     timestamptz DEFAULT now()
);

-- Task 1: allowed_origins per-tenant (уже применено, дубль для полноты)
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS allowed_origins TEXT[] DEFAULT '{}';

-- Для триггеров integration_events (пишутся только для тенантов с parent_integration_id)
ALTER TABLE bot_404_leads ADD COLUMN IF NOT EXISTS assigned_operator_id int;
ALTER TABLE bot_404_leads ADD COLUMN IF NOT EXISTS assigned_contact_id int;
CREATE INDEX IF NOT EXISTS ix_leads_contact ON bot_404_leads(assigned_contact_id)
  WHERE assigned_contact_id IS NOT NULL;

-- Триггер: новый лид → event
CREATE OR REPLACE FUNCTION trg_lead_to_event() RETURNS TRIGGER AS $$
DECLARE v_iid INT;
BEGIN
  SELECT parent_integration_id INTO v_iid FROM tenants WHERE id = NEW.tenant_id;
  IF v_iid IS NOT NULL THEN
    INSERT INTO integration_events (integration_id, tenant_id, actor_id, actor_type, event_type, payload)
    VALUES (v_iid, NEW.tenant_id, NEW.assigned_contact_id,
            CASE WHEN NEW.assigned_contact_id IS NOT NULL THEN 'contact' ELSE NULL END,
            'lead_created',
            jsonb_build_object('lead_id', NEW.id, 'session_id', NEW.session_id, 'phone', NEW.phone, 'status', NEW.status));
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS lead_to_event ON bot_404_leads;
CREATE TRIGGER lead_to_event AFTER INSERT ON bot_404_leads
  FOR EACH ROW EXECUTE FUNCTION trg_lead_to_event();

CREATE OR REPLACE FUNCTION trg_lead_status_to_event() RETURNS TRIGGER AS $$
DECLARE v_iid INT;
BEGIN
  IF OLD.status IS DISTINCT FROM NEW.status THEN
    SELECT parent_integration_id INTO v_iid FROM tenants WHERE id = NEW.tenant_id;
    IF v_iid IS NOT NULL THEN
      INSERT INTO integration_events (integration_id, tenant_id, actor_id, actor_type, event_type, payload)
      VALUES (v_iid, NEW.tenant_id, NEW.assigned_contact_id,
              CASE WHEN NEW.assigned_contact_id IS NOT NULL THEN 'contact' ELSE NULL END,
              'lead_status_changed',
              jsonb_build_object('lead_id', NEW.id, 'old_status', OLD.status, 'new_status', NEW.status));
    END IF;
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS lead_status_to_event ON bot_404_leads;
CREATE TRIGGER lead_status_to_event AFTER UPDATE ON bot_404_leads
  FOR EACH ROW EXECUTE FUNCTION trg_lead_status_to_event();
