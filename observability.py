"""
Observability через LangFuse — трассировка всех вызовов агентов.

Установка:
  pip install langfuse

Получить ключи: https://cloud.langfuse.com (бесплатно)
Или self-hosted: docker compose up -d (один файл)

Добавь в .env:
  LANGFUSE_PUBLIC_KEY=pk-...
  LANGFUSE_SECRET_KEY=sk-...
  LANGFUSE_HOST=https://cloud.langfuse.com  # или localhost:3000 для self-hosted
"""

import os
from functools import wraps
from typing import Any, Callable


def setup_langfuse() -> bool:
    """
    Подключает LangFuse трассировку к OpenAI.
    Вызывай один раз при старте приложения.
    Возвращает True если настроен, False если ключей нет.
    """
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")

    if not (public_key and secret_key):
        print("[observability] LangFuse keys not set — tracing disabled")
        return False

    try:
        from langfuse.openai import openai  # noqa: F401  патчит openai глобально
        from langfuse import Langfuse

        lf = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
        lf.auth_check()
        print("[observability] LangFuse connected — traces available in dashboard")
        return True
    except Exception as e:
        print(f"[observability] LangFuse init failed: {e}")
        return False


def trace_chat(session_id: str, intent: str, message: str, response: str, cost_tokens: int = 0):
    """
    Дополнительная метка в LangFuse — для связи session_id с трассировкой.
    Позволяет фильтровать трассы по сессии в дашборде.
    """
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    if not public_key:
        return

    try:
        from langfuse import Langfuse
        lf = Langfuse()
        lf.score(
            name="chat_turn",
            session_id=session_id,
            value=1,
            comment=f"intent={intent} | tokens={cost_tokens}",
        )
    except Exception:
        pass  # observability не должна ломать основную логику
