-- Пер-тенантные интеграции: кастомный системный промпт + Bitrix24.
-- Секреты (bitrix_webhook) держим ОТДЕЛЬНО от tenant_branding, чтобы не утекли в
-- публичный /api/widget-config и /api/admin/tenants/:slug/branding.
CREATE TABLE IF NOT EXISTS tenant_integrations (
  tenant_id          INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  system_prompt      TEXT,        -- полный кастомный промпт; перекрывает генерик systemPrompt()
  bitrix_webhook     TEXT,        -- базовый URL входящего вебхука: https://<portal>/rest/<uid>/<code>/
  bitrix_source_id   TEXT,        -- SOURCE_ID лида (например код источника «Авито» в воронке клиента)
  bitrix_assigned_by INTEGER,     -- ответственный за лид (ID сотрудника), опционально
  updated_at         TIMESTAMPTZ DEFAULT now()
);
