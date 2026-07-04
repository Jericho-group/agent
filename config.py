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

    # ── Telegram MTProto ──────────────────────────────────────────────────────
    tg_api_id: int = 0
    tg_api_hash: str = ""

    # ── App ───────────────────────────────────────────────────────────────────
    company_name: str = "My Company"
    app_name: str = "MyApp"
    debug: bool = False
    admin_token: str = ""           # обязательно через env ADMIN_TOKEN
    agent_login: str = "agent"
    agent_password: str = ""        # обязательно через env AGENT_PASSWORD
    acc404_login: str = "404ai"
    acc404_password: str = ""       # обязательно через env ACC404_PASSWORD
    bot404_token: str = ""          # обязательно через env BOT404_TOKEN
    internal_api_secret: str = ""   # обязательно через env INTERNAL_API_SECRET

    # ── Router thresholds ─────────────────────────────────────────────────────
    # Если keyword-matching даёт confidence >= threshold → не тратим токены на LLM
    keyword_confidence_threshold: float = 0.75

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()


def _assert_secrets():
    """Падать при старте если критичные секреты пустые / дефолтные / слишком короткие."""
    weak_values = {"changeme", "default", "bot404", "admin", "secret", "password",
                   "aisha-tg-secret-7f3a9b", "test", "123"}
    problems = []
    for name in ("admin_token", "agent_password", "acc404_password", "bot404_token", "internal_api_secret"):
        v = getattr(settings, name, "") or ""
        if not v or v.lower() in weak_values or len(v) < 16:
            problems.append(name.upper())
    if problems:
        import os as _os, sys
        msg = ("[security] weak/default/missing secrets: " + ", ".join(problems)
               + "\n[security] generate with: openssl rand -hex 32")
        if _os.environ.get("DEV_ALLOW_WEAK_SECRETS") == "1":
            sys.stderr.write("[security] WARNING (DEV mode): " + msg + "\n")
        else:
            sys.stderr.write("[security] FATAL: " + msg + "\n")
            sys.exit(1)


_assert_secrets()
