-- ============================================================
-- Supabase Schema для AI Chatbot
-- Запусти это в Supabase Dashboard → SQL Editor → Run
-- ============================================================

-- 1. Включаем расширение для векторного поиска
create extension if not exists vector;

-- ============================================================
-- 2. Таблица истории диалогов
-- ============================================================
create table if not exists messages (
    id          bigserial primary key,
    session_id  text        not null,
    role        text        not null check (role in ('user', 'assistant')),
    content     text        not null,
    created_at  timestamptz not null default now()
);

-- Индекс для быстрой выборки по session_id
create index if not exists idx_messages_session
    on messages (session_id, id);

-- ============================================================
-- 3. Таблица векторной базы знаний
-- ============================================================
create table if not exists knowledge_base (
    id          text        primary key,             -- наш doc id из JSON
    content     text        not null,                -- текст документа
    embedding   vector(1536),                        -- text-embedding-3-small = 1536
    category    text,
    title       text,
    source      text,
    updated_at  timestamptz not null default now()
);

-- HNSW индекс для быстрого приближённого поиска (cosine distance)
create index if not exists idx_kb_embedding
    on knowledge_base
    using hnsw (embedding vector_cosine_ops)
    with (m = 16, ef_construction = 64);

-- ============================================================
-- 4. Таблица few-shot примеров (для обучения / коррекции)
-- ============================================================
create table if not exists few_shot_examples (
    id          bigserial primary key,
    intent      text        not null,
    user_msg    text        not null,
    bad_answer  text,                                -- что ответил бот
    good_answer text        not null,                -- что исправил менеджер
    created_at  timestamptz not null default now()
);

-- ============================================================
-- 5. Row Level Security (RLS) — необязательно для MVP,
--    но рекомендуется для продакшена
-- ============================================================
-- По умолчанию отключаем RLS, т.к. обращаемся через service_role key
alter table messages         disable row level security;
alter table knowledge_base   disable row level security;
alter table few_shot_examples disable row level security;

-- ============================================================
-- 6. Хелпер-функция для семантического поиска
--    Вызывается из Python: supabase.rpc('search_knowledge', {...})
-- ============================================================
create or replace function search_knowledge(
    query_embedding vector(1536),
    match_count     int     default 5,
    filter_category text    default null
)
returns table (
    id          text,
    content     text,
    category    text,
    title       text,
    similarity  float
)
language plpgsql
as $$
begin
    return query
    select
        kb.id,
        kb.content,
        kb.category,
        kb.title,
        1 - (kb.embedding <=> query_embedding) as similarity
    from knowledge_base kb
    where
        (filter_category is null or kb.category = filter_category)
        and kb.embedding is not null
    order by kb.embedding <=> query_embedding
    limit match_count;
end;
$$;
