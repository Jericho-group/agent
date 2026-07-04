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

-- ============================================================
-- Proactive Outreach Module
-- ============================================================

-- Telegram аккаунты (сессии)
CREATE TABLE IF NOT EXISTS tg_accounts (
    id          BIGSERIAL PRIMARY KEY,
    phone       TEXT NOT NULL UNIQUE,
    session_str TEXT,                          -- Telethon StringSession
    status      TEXT NOT NULL DEFAULT 'pending', -- pending|active|banned|paused
    daily_limit INT  NOT NULL DEFAULT 30,
    sent_today  INT  NOT NULL DEFAULT 0,
    last_reset  DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Кампании
CREATE TABLE IF NOT EXISTS campaigns (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft', -- draft|active|paused|done
    account_id   BIGINT REFERENCES tg_accounts(id),
    first_message TEXT NOT NULL,               -- шаблон первого сообщения
    goal         TEXT,                          -- цель для оркестратора
    delay_min    INT  NOT NULL DEFAULT 60,      -- мин. задержка между сообщениями (сек)
    delay_max    INT  NOT NULL DEFAULT 180,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Контакты кампании
CREATE TABLE IF NOT EXISTS campaign_contacts (
    id          BIGSERIAL PRIMARY KEY,
    campaign_id BIGINT REFERENCES campaigns(id) ON DELETE CASCADE,
    username    TEXT,
    phone       TEXT,
    name        TEXT,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending|sent|replied|converted|failed
    session_id  TEXT UNIQUE,                     -- session_id для памяти оркестратора
    sent_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Очередь отправки
CREATE TABLE IF NOT EXISTS outreach_queue (
    id          BIGSERIAL PRIMARY KEY,
    contact_id  BIGINT REFERENCES campaign_contacts(id) ON DELETE CASCADE,
    campaign_id BIGINT REFERENCES campaigns(id) ON DELETE CASCADE,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status      TEXT NOT NULL DEFAULT 'pending', -- pending|sent|failed
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Лог диалогов (входящие/исходящие)
CREATE TABLE IF NOT EXISTS outreach_messages (
    id          BIGSERIAL PRIMARY KEY,
    contact_id  BIGINT REFERENCES campaign_contacts(id) ON DELETE CASCADE,
    direction   TEXT NOT NULL,                   -- out|in
    content     TEXT NOT NULL,
    tg_msg_id   BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
