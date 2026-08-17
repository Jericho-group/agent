-- Fix: колонки ротации credential для integrations (v1.1 § 4.7)
-- endpoint /prm/api/integration/rotate-credential использует api_credential_hash_prev
ALTER TABLE integrations ADD COLUMN IF NOT EXISTS api_credential_hash_prev text;
ALTER TABLE integrations ADD COLUMN IF NOT EXISTS api_credential_prev_expires_at timestamptz;
