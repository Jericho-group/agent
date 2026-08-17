-- Task 9: исходящий webhook для integration_events
ALTER TABLE integration_events ADD COLUMN IF NOT EXISTS webhook_delivered_at timestamptz;
ALTER TABLE integration_events ADD COLUMN IF NOT EXISTS webhook_attempts int DEFAULT 0;
ALTER TABLE integration_events ADD COLUMN IF NOT EXISTS webhook_last_error text;

CREATE INDEX IF NOT EXISTS ix_events_webhook_pending
  ON integration_events(integration_id, id)
  WHERE webhook_delivered_at IS NULL AND webhook_attempts < 5;

-- webhook_secret для HMAC (пока плейнтекст в MVP; шифровать AES-GCM в v2)
ALTER TABLE integrations ADD COLUMN IF NOT EXISTS webhook_secret text;
