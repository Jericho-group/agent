-- ─────────────────────────────────────────────────────────────────
-- PRM v1.2 Task 1: allowed_origins per-tenant (T1 из PRM ответов)
-- «У нас у каждого тенанта свой домен» — CSP frame-ancestors per-tenant,
-- fallback на integrations.allowed_origins если у tenant пусто.
-- Идемпотентно. Заря (tenant_id=6, parent_integration_id IS NULL) не задета.
-- ─────────────────────────────────────────────────────────────────

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS allowed_origins TEXT[] DEFAULT '{}';

-- Индекс для быстрого lookup по домену (при валидации Origin в /admin/api/embed/*)
CREATE INDEX IF NOT EXISTS ix_tenants_allowed_origins
  ON tenants USING GIN (allowed_origins) WHERE array_length(allowed_origins, 1) > 0;

-- Task 4 (заранее): parent_contact_id для sub-partners в v2 (сейчас NULL для всех)
-- ALTER TABLE не выполняем пока integrations/tenant_contacts не существуют в prod
-- (эти таблицы будут созданы Task 2/3, здесь только напоминание)
