"""
Dialogue Memory — хранилище истории диалогов в Supabase (PostgreSQL).

Использует asyncpg для async-доступа из FastAPI.
Таблица `messages` создаётся через supabase/schema.sql
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from config import settings

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

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        intent: str | None = None,
    ) -> None:
        pool = await _get_pool()
        await pool.execute(
            """
            INSERT INTO messages (session_id, role, content, intent)
            VALUES ($1, $2, $3, $4)
            """,
            session_id,
            role,
            content,
            intent,
        )

    async def clear_session(self, session_id: str) -> None:
        pool = await _get_pool()
        await pool.execute(
            "DELETE FROM messages WHERE session_id = $1",
            session_id,
        )
        await pool.execute(
            "DELETE FROM session_meta WHERE session_id = $1",
            session_id,
        )

    # ── Read ─────────────────────────────────────────────────────────────────

    async def get_history(
        self,
        session_id: str,
        limit: int | None = None,
    ) -> list[dict]:
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
        pool = await _get_pool()
        rows = await pool.fetch(
            "SELECT DISTINCT session_id FROM messages"
        )
        return [row["session_id"] for row in rows]

    def format_for_agent(self, history: list[dict]) -> str:
        if not history:
            return "Начало диалога."
        lines = []
        for msg in history:
            role_label = "Клиент" if msg["role"] == "user" else "Ассистент"
            lines.append(f"{role_label}: {msg['content']}")
        return "\n".join(lines)

    # ── Session meta (BANT / escalation / NPS) ───────────────────────────────

    async def upsert_session_meta(
        self,
        session_id: str,
        *,
        bant: dict | None = None,
        escalated: bool | None = None,
        nps_score: int | None = None,
        tags: list[str] | None = None,
    ) -> None:
        pool = await _get_pool()
        # Сначала гарантируем наличие строки
        await pool.execute(
            """
            INSERT INTO session_meta (session_id)
            VALUES ($1)
            ON CONFLICT (session_id) DO NOTHING
            """,
            session_id,
        )
        if bant is not None:
            await pool.execute(
                "UPDATE session_meta SET bant = $2, updated_at = now() WHERE session_id = $1",
                session_id,
                json.dumps(bant, ensure_ascii=False),
            )
        if escalated is not None:
            await pool.execute(
                "UPDATE session_meta SET escalated = $2, updated_at = now() WHERE session_id = $1",
                session_id,
                escalated,
            )
        if nps_score is not None:
            await pool.execute(
                "UPDATE session_meta SET nps_score = $2, updated_at = now() WHERE session_id = $1",
                session_id,
                nps_score,
            )
        if tags is not None:
            await pool.execute(
                "UPDATE session_meta SET tags = $2, updated_at = now() WHERE session_id = $1",
                session_id,
                tags,
            )

    async def get_session_meta(self, session_id: str) -> dict[str, Any]:
        pool = await _get_pool()
        row = await pool.fetchrow(
            "SELECT bant, escalated, nps_score, tags, updated_at::text FROM session_meta WHERE session_id = $1",
            session_id,
        )
        if not row:
            return {"bant": {}, "escalated": False, "nps_score": None, "tags": [], "updated_at": None}
        bant = row["bant"]
        if isinstance(bant, str):
            try:
                bant = json.loads(bant)
            except Exception:
                bant = {}
        return {
            "bant": bant or {},
            "escalated": row["escalated"],
            "nps_score": row["nps_score"],
            "tags": list(row["tags"] or []),
            "updated_at": row["updated_at"],
        }

    # ── Analytics ────────────────────────────────────────────────────────────

    async def get_stats(self) -> dict[str, Any]:
        pool = await _get_pool()
        total_sessions = await pool.fetchval("SELECT COUNT(DISTINCT session_id) FROM messages")
        total_messages = await pool.fetchval("SELECT COUNT(*) FROM messages")
        messages_24h = await pool.fetchval(
            "SELECT COUNT(*) FROM messages WHERE created_at > now() - interval '24 hours'"
        )
        escalated = await pool.fetchval("SELECT COUNT(*) FROM session_meta WHERE escalated")
        qualified = await pool.fetchval(
            "SELECT COUNT(*) FROM session_meta WHERE (bant->>'qualified')::boolean IS TRUE"
        )
        hot = await pool.fetchval("SELECT COUNT(*) FROM session_meta WHERE bant->>'temperature' = 'hot'")
        warm = await pool.fetchval("SELECT COUNT(*) FROM session_meta WHERE bant->>'temperature' = 'warm'")
        cold = await pool.fetchval("SELECT COUNT(*) FROM session_meta WHERE bant->>'temperature' = 'cold'")

        intent_rows = await pool.fetch(
            """
            SELECT intent, COUNT(*) AS n
            FROM messages
            WHERE role = 'user' AND intent IS NOT NULL
            GROUP BY intent
            ORDER BY n DESC
            """
        )
        intents = [{"intent": r["intent"], "count": r["n"]} for r in intent_rows]

        avg_msgs = 0.0
        if total_sessions:
            avg_msgs = round((total_messages or 0) / total_sessions, 2)

        return {
            "total_sessions":   total_sessions or 0,
            "total_messages":   total_messages or 0,
            "messages_24h":     messages_24h or 0,
            "avg_msgs_per_session": avg_msgs,
            "escalated":        escalated or 0,
            "qualified_leads":  qualified or 0,
            "temperature": {
                "hot":  hot or 0,
                "warm": warm or 0,
                "cold": cold or 0,
            },
            "intents": intents,
        }

    async def get_sessions_enriched(self) -> list[dict]:
        """Список сессий с количеством сообщений + метаданными."""
        pool = await _get_pool()
        rows = await pool.fetch(
            """
            SELECT m.session_id,
                   COUNT(*) AS msg_count,
                   MAX(m.created_at)::text AS last_msg_at,
                   sm.escalated,
                   sm.bant,
                   sm.nps_score
            FROM messages m
            LEFT JOIN session_meta sm ON sm.session_id = m.session_id
            GROUP BY m.session_id, sm.escalated, sm.bant, sm.nps_score
            ORDER BY MAX(m.created_at) DESC
            """
        )
        result = []
        for r in rows:
            bant = r["bant"]
            if isinstance(bant, str):
                try:
                    bant = json.loads(bant)
                except Exception:
                    bant = {}
            result.append({
                "session_id":  r["session_id"],
                "message_count": r["msg_count"],
                "last_msg_at": r["last_msg_at"],
                "escalated":   bool(r["escalated"]),
                "bant":        bant or {},
                "nps_score":   r["nps_score"],
            })
        return result

    # ── Few-shot ─────────────────────────────────────────────────────────────

    async def save_correction(
        self,
        intent: str,
        user_msg: str,
        bad_answer: str,
        good_answer: str,
    ) -> None:
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
