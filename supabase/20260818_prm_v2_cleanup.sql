-- Cleanup: PRM iframe SSO v2 (заменён v1.1)
-- Safety: 0 использованных JWT, только тестовый партнёр id=3 (наш собственный),
--         только 1 тестовый тенант id=9 slug=prm-3-5d117c13 (можно удалить).

BEGIN;

-- 1. Удаляем тестовый тенант id=9 (создан через v2 bootstrap для demo)
DELETE FROM tenants WHERE id = 9 AND slug = 'prm-3-5d117c13' AND parent_prm_partner_id = 3;

-- 2. DROP TABLE v2-таблицы (с CASCADE на FK)
DROP TABLE IF EXISTS prm_partner_api_keys CASCADE;
DROP TABLE IF EXISTS prm_sso_jti_used CASCADE;
DROP TABLE IF EXISTS prm_audit_log CASCADE;
DROP TABLE IF EXISTS prm_partners CASCADE;

-- 3. DROP колонки tenants (v2-специфичные)
ALTER TABLE tenants DROP COLUMN IF EXISTS parent_prm_partner_id;
ALTER TABLE tenants DROP COLUMN IF EXISTS prm_external_id;

COMMIT;
