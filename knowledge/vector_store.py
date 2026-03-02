"""
Vector Store — обёртка над Supabase pgvector.

Используется для хранения и поиска по базе знаний компании.
Embeddings генерирует OpenAI (или совместимый провайдер).

Таблица `knowledge_base` и функция `search_knowledge`
создаются через supabase/schema.sql
"""

from __future__ import annotations

import psycopg2
import psycopg2.extras
from openai import OpenAI

from config import settings

# ── Sync PostgreSQL соединение (для CrewAI tools, которые sync) ──────────────
# psycopg2 т.к. CrewAI _run() — синхронный
_conn: psycopg2.extensions.connection | None = None


def _get_conn() -> psycopg2.extensions.connection:
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(dsn=settings.supabase_db_url)
        _conn.autocommit = True
    return _conn


def _embed(text: str) -> list[float]:
    """Генерирует embedding через OpenAI API."""
    client = OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    response = client.embeddings.create(
        model=settings.embedding_model,
        input=text,
    )
    return response.data[0].embedding


class VectorStore:
    """Синглтон-обёртка над Supabase pgvector."""

    _instance: VectorStore | None = None

    def __new__(cls) -> VectorStore:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ── Write ────────────────────────────────────────────────────────────────

    def upsert(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict] | None = None,
    ) -> None:
        """
        Добавляет или обновляет документы.
        Генерирует embeddings для каждого документа через OpenAI.
        """
        conn = _get_conn()
        metas = metadatas or [{} for _ in ids]

        for doc_id, doc_text, meta in zip(ids, documents, metas):
            embedding = _embed(doc_text)
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO knowledge_base (id, content, embedding, category, title, source)
                    VALUES (%s, %s, %s::vector, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        content   = EXCLUDED.content,
                        embedding = EXCLUDED.embedding,
                        category  = EXCLUDED.category,
                        title     = EXCLUDED.title,
                        source    = EXCLUDED.source,
                        updated_at = now()
                    """,
                    (
                        doc_id,
                        doc_text,
                        embedding_str,
                        meta.get("category"),
                        meta.get("title"),
                        meta.get("source"),
                    ),
                )

    def delete_all(self) -> None:
        """Очищает всю коллекцию (для ре-индексации)."""
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM knowledge_base")

    # ── Read ─────────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        n_results: int = 5,
        where: dict | None = None,
    ) -> list[dict]:
        """
        Семантический поиск по базе знаний через pgvector.
        Возвращает список dict с ключами: document, metadata, distance.
        """
        if self.count() == 0:
            return []

        query_embedding = _embed(query)
        embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

        category = where.get("category") if where else None

        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, content, category, title, similarity
                FROM search_knowledge(
                    %s::vector,
                    %s,
                    %s
                )
                """,
                (embedding_str, n_results, category),
            )
            rows = cur.fetchall()

        return [
            {
                "document": row["content"],
                "metadata": {"category": row["category"], "title": row["title"]},
                "distance": 1.0 - float(row["similarity"]),  # cosine distance
            }
            for row in rows
        ]

    def count(self) -> int:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM knowledge_base")
            result = cur.fetchone()
        return result[0] if result else 0
