"""
RAG Search Tool — инструмент для поиска по базе знаний компании.

Агенты CrewAI вызывают этот инструмент когда им нужно найти
информацию о продукте, ценах, FAQ или руководствах.
"""

from pydantic import BaseModel, Field

from crewai.tools import BaseTool
from knowledge.vector_store import VectorStore


class RAGSearchInput(BaseModel):
    query: str = Field(
        description=(
            "Поисковый запрос для нахождения релевантной информации "
            "в базе знаний компании. Пиши запрос как вопрос или ключевые слова."
        )
    )
    category: str | None = Field(
        default=None,
        description=(
            "Необязательный фильтр по категории: "
            "'features', 'pricing', 'howto', 'faq', 'sales_scripts'. "
            "Если не указан — ищет по всем категориям."
        ),
    )
    n_results: int = Field(default=4, ge=1, le=10, description="Количество результатов.")


class RAGSearchTool(BaseTool):
    name: str = "Knowledge Base Search"
    description: str = (
        "Ищет информацию в базе знаний компании. "
        "Используй для ответов на вопросы о продуктах, ценах, функциях, "
        "руководствах и FAQ. Всегда проверяй базу знаний перед ответом."
    )
    args_schema: type[BaseModel] = RAGSearchInput

    def _run(self, query: str, category: str | None = None, n_results: int = 4) -> str:
        store = VectorStore()

        if store.count() == 0:
            return (
                "База знаний пуста. Запусти скрипт ingest_data.py для загрузки данных."
            )

        where = {"category": category} if category else None
        results = store.search(query=query, n_results=n_results, where=where)

        if not results:
            return "По данному запросу ничего не найдено в базе знаний."

        # Фильтруем слишком далёкие результаты (cosine distance > 0.7)
        relevant = [r for r in results if r["distance"] < 0.7]

        if not relevant:
            return "Релевантной информации не найдено. Возможно, этот вопрос выходит за рамки базы знаний."

        parts = []
        for i, r in enumerate(relevant, 1):
            meta = r["metadata"]
            title = meta.get("title", "")
            cat = meta.get("category", "")
            header = f"[{i}] {title} ({cat})" if title else f"[{i}]"
            parts.append(f"{header}\n{r['document']}")

        return "\n\n---\n\n".join(parts)
