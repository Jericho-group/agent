-- Харденинг изоляции тенантов (2026-07). Эти объекты живут в БД (применялись
-- миграциями напрямую), в schema.sql их нет или устаревшие версии. Файл фиксирует
-- ТЕКУЩИЕ безопасные определения — применять на свежую БД после базовой схемы.

-- 1) RAG строго по тенанту: убран OR tenant_id IS NULL (чанки без тенанта больше
--    не видны всем; было потенциальной утечкой при ingest без tenant_id).
CREATE OR REPLACE FUNCTION public.search_knowledge(
    query_embedding vector, p_tenant_id integer,
    match_count integer DEFAULT 5, filter_category text DEFAULT NULL::text)
 RETURNS TABLE(id text, content text, category text, title text, similarity double precision)
 LANGUAGE plpgsql AS $function$
begin
    return query
    select kb.id, kb.content, kb.category, kb.title, 1 - (kb.embedding <=> query_embedding) as similarity
    from knowledge_base kb
    where kb.tenant_id = p_tenant_id
        and (filter_category is null or kb.category = filter_category)
        and kb.embedding is not null
    order by kb.embedding <=> query_embedding
    limit match_count;
end; $function$;

-- 2) Брендинг без 404ai-дефолтов: brand_name → имя тенанта, manager_email → NULL
--    (было '404ai' и 'ap@404ai.ru' — течёт в widget-config чужим тенантам).
--    Внутренним 404ai-тенантам (1,2,3) брендинг проставлен явно в tenant_branding.
CREATE OR REPLACE VIEW v_tenant_branding AS
 SELECT t.id AS tenant_id, t.slug,
    COALESCE(b.brand_name, t.name) AS brand_name,
    COALESCE(b.bot_name, 'Ассистент'::text) AS bot_name,
    COALESCE(b.role_subtitle, 'AI-консультант · обычно отвечает сразу'::text) AS role_subtitle,
    b.logo_url,
    COALESCE(b.primary_color, '#0B8A5B'::text) AS primary_color,
    COALESCE(b.accent_color, '#E0B341'::text) AS accent_color,
    COALESCE(b.text_color, '#1A1A1A'::text) AS text_color,
    COALESCE(b.greeting, 'Здравствуйте! Чем могу помочь?'::text) AS greeting,
    COALESCE(b.nudge_text, 'Нужна помощь?'::text) AS nudge_text,
    b.chat_title, b.footer_text,
    b.manager_email AS manager_email,
    COALESCE(b."position", 'br'::text) AS "position",
    COALESCE(b.font_family, 'Manrope'::text) AS font_family,
    b.custom_css, b.updated_at
   FROM tenants t LEFT JOIN tenant_branding b ON b.tenant_id = t.id;
