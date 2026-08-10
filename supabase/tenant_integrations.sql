-- Пер-тенантные интеграции: кастомный системный промпт + Bitrix24.
-- Секреты (bitrix_webhook) держим ОТДЕЛЬНО от tenant_branding, чтобы не утекли в
-- публичный /api/widget-config и /api/admin/tenants/:slug/branding.
CREATE TABLE IF NOT EXISTS tenant_integrations (
  tenant_id          INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  system_prompt      TEXT,        -- полный кастомный промпт; перекрывает генерик systemPrompt()
  bitrix_webhook     TEXT,        -- базовый URL входящего вебхука: https://<portal>/rest/<uid>/<code>/
  bitrix_source_id   TEXT,        -- SOURCE_ID лида (например код источника «Авито» в воронке клиента)
  bitrix_assigned_by INTEGER,     -- ответственный за лид (ID сотрудника), опционально
  avito_client_id     TEXT,       -- Avito Messenger API: OAuth client_id
  avito_client_secret TEXT,       -- Avito Messenger API: OAuth client_secret
  avito_user_id       TEXT,       -- Avito account user_id (по нему webhook → тенант)
  updated_at         TIMESTAMPTZ DEFAULT now()
);

-- Идемпотентно для уже существующих установок:
ALTER TABLE tenant_integrations ADD COLUMN IF NOT EXISTS avito_client_id     TEXT;
ALTER TABLE tenant_integrations ADD COLUMN IF NOT EXISTS avito_client_secret TEXT;
ALTER TABLE tenant_integrations ADD COLUMN IF NOT EXISTS avito_user_id       TEXT;
