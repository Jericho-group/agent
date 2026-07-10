-- ============================================================================
-- Миграция: PRM iframe SSO (2026-07)
-- Реализует ТЗ TZ_PRM_iframe_SSO-2.docx:
--   • prm_partners           — реестр партнёров-заказчиков (PRM-платформы)
--   • prm_partner_api_keys   — API-ключи с поддержкой ротации (два ключа одновременно)
--   • tenants.parent_prm_partner_id + tenants.prm_external_id — принадлежность к партнёру
--   • prm_sso_jti_used       — защита от replay одноразового JWT (TTL 60 сек + запас)
--   • prm_audit_log          — аудит всех /prm/api/* операций (90 дней хранения)
--
-- ПРИНЦИП: миграция аддитивная. Ни одна существующая таблица не пересоздаётся,
-- существующие колонки не меняются. Все новые колонки nullable → старые тенанты
-- (aisha/orchestra/prm/zarya/echolytics/default/test-tenant-b) не затронуты.
--
-- Именование: префикс prm_ отличает от существующей рефералки Аиши
-- (bot_404_partners / bot_404_partner_leads) — это разные подсистемы.
-- ============================================================================

-- ── Реестр партнёров-заказчиков (PRM-платформ, встраивающих Дирижёр iframe) ──
CREATE TABLE IF NOT EXISTS prm_partners (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,                    -- «PRM Online»
    contact_email TEXT,                             -- ответственный разработчик
    allowed_origins TEXT[] NOT NULL DEFAULT '{}',   -- ['https://cabinet.prmonline.ru', 'https://dev.prmonline.ru']
    webhook_url   TEXT,                             -- v2: куда слать события (lead.created и т.д.)
    webhook_secret TEXT,                            -- секрет для HMAC-подписи исходящих webhook
    status        TEXT NOT NULL DEFAULT 'active'    -- active / paused / revoked
                  CHECK (status IN ('active', 'paused', 'revoked')),
    default_plan_id INTEGER REFERENCES plans(id),   -- какой plan получают новые sub-тенанты партнёра
    default_modules JSONB DEFAULT '{"widget":true,"telegram":true}'::jsonb,  -- какие каналы включены
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_prm_partners_status
    ON prm_partners(status) WHERE status = 'active';


-- ── API-ключи партнёра (отдельная таблица для поддержки ротации) ─────────────
-- Партнёр аутентифицируется на /prm/api/* через Authorization: Bearer prm_<hex>
-- В БД храним bcrypt-hash полного ключа. Префикс — для быстрого lookup
-- по первым 12 символам (без секретной части).
--
-- При ротации: старый ключ переводится в status='rotating' на 24 часа,
-- одновременно активен новый. После — старый в 'revoked'.
CREATE TABLE IF NOT EXISTS prm_partner_api_keys (
    id           SERIAL PRIMARY KEY,
    partner_id   INTEGER NOT NULL REFERENCES prm_partners(id) ON DELETE CASCADE,
    key_prefix   TEXT NOT NULL UNIQUE,        -- 'prm_' + первые 8 символов hex (для быстрого lookup)
    key_hash     TEXT NOT NULL,               -- bcrypt(полный_ключ)
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'rotating', 'revoked')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at   TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ                  -- обновляется при валидации, для мониторинга
);

CREATE INDEX IF NOT EXISTS idx_prm_partner_api_keys_lookup
    ON prm_partner_api_keys(key_prefix)
    WHERE status IN ('active', 'rotating');

CREATE INDEX IF NOT EXISTS idx_prm_partner_api_keys_partner
    ON prm_partner_api_keys(partner_id);


-- ── Связь тенант ↔ партнёр ───────────────────────────────────────────────────
-- Каждый sub-тенант (конечный пользователь партнёра) принадлежит одному партнёру.
-- Старые тенанты (aisha/orchestra/prm/zarya) → parent_prm_partner_id=NULL,
-- поведение не меняется.
--
-- prm_external_id — внешний идентификатор пользователя на стороне партнёра.
-- Партнёр использует его для API-запросов; уникален в рамках одного партнёра.
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS parent_prm_partner_id INTEGER
        REFERENCES prm_partners(id) ON DELETE RESTRICT;

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS prm_external_id TEXT;

-- Уникальность: у одного партнёра не может быть двух sub-тенантов с одинаковым external_id.
-- Частичный индекс — не мешает существующим тенантам, где оба поля NULL.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tenants_prm_external_id
    ON tenants(parent_prm_partner_id, prm_external_id)
    WHERE parent_prm_partner_id IS NOT NULL AND prm_external_id IS NOT NULL;


-- ── Защита от replay одноразового SSO-JWT ────────────────────────────────────
-- JWT имеет jti (uuid4). При валидации в /embed — insert jti; при повторном
-- использовании того же jti — конфликт по PK → 401.
-- Cleanup: cron раз в час удаляет строки с expires_at < now().
CREATE TABLE IF NOT EXISTS prm_sso_jti_used (
    jti         UUID PRIMARY KEY,
    partner_id  INTEGER NOT NULL REFERENCES prm_partners(id) ON DELETE CASCADE,
    tenant_id   INTEGER REFERENCES tenants(id),    -- какого sub-тенанта открывали
    used_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,               -- iat + exp (обычно now() + 60 сек)
    ip          INET
);

CREATE INDEX IF NOT EXISTS idx_prm_sso_jti_expiry
    ON prm_sso_jti_used(expires_at);


-- ── Аудит-лог PRM-операций ───────────────────────────────────────────────────
-- Пишем каждый вызов /prm/api/* и /embed для мониторинга/безопасности.
-- ТЗ: хранение 90 дней. Cleanup — периодическая задача.
CREATE TABLE IF NOT EXISTS prm_audit_log (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    partner_id   INTEGER REFERENCES prm_partners(id) ON DELETE SET NULL,
    action       TEXT NOT NULL,                    -- partner.create / partner.pause / sso.issue / sso.replay_blocked / ...
    target_tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL,
    ip           INET,
    user_agent   TEXT,
    request_id   UUID,                             -- для корреляции нескольких строк одного запроса
    status       TEXT,                             -- success / error
    error_code   TEXT,                             -- при ошибках: bad_key / not_found / rate_limited / replay / ...
    meta         JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_prm_audit_partner_ts
    ON prm_audit_log(partner_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_prm_audit_ts
    ON prm_audit_log(ts DESC);

CREATE INDEX IF NOT EXISTS idx_prm_audit_action
    ON prm_audit_log(action, ts DESC);


-- ── Комментарии таблиц (для будущего разработчика) ───────────────────────────
COMMENT ON TABLE prm_partners IS
    'Реестр партнёров-заказчиков Дирижёра (PRM-платформ, встраивающих ЛК через iframe SSO). Не путать с bot_404_partners (реферальная программа Аиши).';

COMMENT ON TABLE prm_partner_api_keys IS
    'API-ключи партнёров с поддержкой ротации: два ключа одновременно (active + rotating) на переходный период.';

COMMENT ON COLUMN tenants.parent_prm_partner_id IS
    'Партнёр, которому принадлежит sub-тенант. NULL для внутренних тенантов Исполнителя (aisha/orchestra/prm/zarya и т.д.).';

COMMENT ON COLUMN tenants.prm_external_id IS
    'Идентификатор пользователя на стороне партнёра (для маппинга при синхронизации). Уникален в рамках одного партнёра.';

COMMENT ON TABLE prm_sso_jti_used IS
    'Использованные jti одноразовых SSO-JWT. Защита от replay-атак. Cleanup: expires_at < now().';

COMMENT ON TABLE prm_audit_log IS
    'Аудит всех операций /prm/api/* и /embed. Хранение 90 дней (по ТЗ). Cleanup: ts < now() - 90 days.';
