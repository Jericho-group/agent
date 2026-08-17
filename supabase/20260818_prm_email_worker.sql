-- Task 5: email-нотификации offline-акторам (T6 из PRM ответов)
-- Событие считается "notified" когда email отправлен успешно.
ALTER TABLE integration_events ADD COLUMN IF NOT EXISTS email_sent_at timestamptz;
CREATE INDEX IF NOT EXISTS ix_events_email_pending
  ON integration_events(integration_id, actor_id, created_at)
  WHERE email_sent_at IS NULL AND actor_id IS NOT NULL;

-- Настройка per-integration: адрес отправителя и включен ли email
ALTER TABLE integrations ADD COLUMN IF NOT EXISTS email_enabled bool DEFAULT true;
ALTER TABLE integrations ADD COLUMN IF NOT EXISTS email_from text;
