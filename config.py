from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── LLM ──────────────────────────────────────────────────────────────────
    openai_api_key: str = "your-api-key-here"
    openai_base_url: str = "https://api.openai.com/v1"

    # Модели: тяжёлые агенты vs лёгкий роутер
    orchestrator_model: str = "gpt-4o"        # для сложных цепочек (CrewAI crews)
    agent_model: str = "gpt-4o-mini"          # для агентов
    router_model: str = "gpt-4o-mini"         # для классификации интента (быстро/дёшево)
    embedding_model: str = "text-embedding-3-small"

    # ── Supabase ──────────────────────────────────────────────────────────────
    # Найти в: Supabase Dashboard → Settings → Database → Connection string
    # Используй "Transaction pooler" (порт 6543) если Supabase hosted
    # Используй "Direct connection" (порт 5432) если self-hosted
    supabase_db_url: str = (
        "postgresql://postgres.[project-ref]:[password]"
        "@aws-0-eu-central-1.pooler.supabase.com:6543/postgres"
    )
    # Для Supabase Python SDK (опционально, для Storage/Auth):
    supabase_url: str = "https://[project-ref].supabase.co"
    supabase_service_key: str = "your-service-role-key"   # из Settings → API → service_role

    # ── Dialogue Memory ───────────────────────────────────────────────────────
    max_history_messages: int = 10  # сколько последних сообщений передавать агенту

    # ── App ───────────────────────────────────────────────────────────────────
    company_name: str = "My Company"
    app_name: str = "MyApp"
    debug: bool = False
    admin_token: str = "changeme"  # поменяй в .env → ADMIN_TOKEN=ваш_пароль

    # ── Router thresholds ─────────────────────────────────────────────────────
    # Если keyword-matching даёт confidence >= threshold → не тратим токены на LLM
    keyword_confidence_threshold: float = 0.75

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
