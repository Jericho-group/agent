-- ALTER на существующие таблицы (idempotent)
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS parent_integration_id int REFERENCES integrations(id);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS external_id text;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS delete_scheduled_at timestamptz;
CREATE UNIQUE INDEX IF NOT EXISTS ux_tenants_ext
  ON tenants(parent_integration_id, external_id)
  WHERE parent_integration_id IS NOT NULL AND external_id IS NOT NULL;

ALTER TABLE bot_404_leads ADD COLUMN IF NOT EXISTS assigned_operator_id int;
ALTER TABLE bot_404_leads ADD COLUMN IF NOT EXISTS assigned_contact_id int;
CREATE INDEX IF NOT EXISTS ix_leads_contact ON bot_404_leads(assigned_contact_id) WHERE assigned_contact_id IS NOT NULL;
