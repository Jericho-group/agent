"""
Dialogue Memory — хранилище истории диалогов в Supabase (PostgreSQL).

Использует asyncpg для async-доступа из FastAPI.
Таблица `messages` создаётся через supabase/schema.sql
"""

from __future__ import annotations

import asyncpg

from config import settings

# Пул соединений — создаётся один раз при первом обращении
_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.supabase_db_url,
            min_size=1,
            max_size=10,
            command_timeout=30,
        )
    return _pool


class DialogueMemory:
    """Async PostgreSQL-хранилище истории диалогов через Supabase."""

    # ── Write ────────────────────────────────────────────────────────────────

    async def add_message(self, session_id: str, role: str, content: str) -> None:
        pool = await _get_pool()
        await pool.execute(
            """
            INSERT INTO messages (session_id, role, content)
            VALUES ($1, $2, $3)
            """,
            session_id,
            role,
            content,
        )

    async def clear_session(self, session_id: str) -> None:
        pool = await _get_pool()
        await pool.execute(
            "DELETE FROM messages WHERE session_id = $1",
            session_id,
        )

    # ── Read ─────────────────────────────────────────────────────────────────

    async def get_history(
        self,
        session_id: str,
        limit: int | None = None,
    ) -> list[dict]:
        """
        Возвращает список dict {"role": ..., "content": ..., "created_at": ...}
        в хронологическом порядке.
        """
        max_msgs = limit or settings.max_history_messages
        pool = await _get_pool()

        rows = await pool.fetch(
            """
            SELECT role, content, created_at::text
            FROM (
                SELECT role, content, created_at, id
                FROM messages
                WHERE session_id = $1
                ORDER BY id DESC
                LIMIT $2
            ) sub
            ORDER BY id
            """,
            session_id,
            max_msgs,
        )

        return [
            {"role": row["role"], "content": row["content"], "created_at": row["created_at"]}
            for row in rows
        ]

    async def get_all_sessions(self) -> list[str]:
        """Список всех уникальных session_id (для /health)."""
        pool = await _get_pool()
        rows = await pool.fetch(
            "SELECT DISTINCT session_id FROM messages"
        )
        return [row["session_id"] for row in rows]

    def format_for_agent(self, history: list[dict]) -> str:
        """Форматирует историю в виде читаемого текста для промпта агента."""
        if not history:
            return "Начало диалога."
        lines = []
        for msg in history:
            role_label = "Клиент" if msg["role"] == "user" else "Ассистент"
            lines.append(f"{role_label}: {msg['content']}")
        return "\n".join(lines)

    # ── Few-shot (обучение по корректировкам менеджера) ───────────────────────

    async def save_correction(
        self,
        intent: str,
        user_msg: str,
        bad_answer: str,
        good_answer: str,
    ) -> None:
        """
        Сохраняет исправление менеджера в few_shot_examples.
        Вызывается из POST /admin/correct эндпоинта.
        """
        pool = await _get_pool()
        await pool.execute(
            """
            INSERT INTO few_shot_examples (intent, user_msg, bad_answer, good_answer)
            VALUES ($1, $2, $3, $4)
            """,
            intent,
            user_msg,
            bad_answer,
            good_answer,
        )

    async def get_few_shot_examples(self, intent: str, limit: int = 3) -> list[dict]:
        """
        Возвращает примеры исправлений для конкретного интента.
        Используются в промпте агента чтобы бот учился на ошибках.
        """
        pool = await _get_pool()
        rows = await pool.fetch(
            """
            SELECT user_msg, good_answer
            FROM few_shot_examples
            WHERE intent = $1
            ORDER BY id DESC
            LIMIT $2
            """,
            intent,
            limit,
        )
        return [
            {"user_msg": row["user_msg"], "good_answer": row["good_answer"]}
            for row in rows
        ]
