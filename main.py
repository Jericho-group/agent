"""
FastAPI приложение — основной API чат-бота.

Эндпоинты:
  POST /chat                    — отправить сообщение
  GET  /history/{session_id}   — история сессии
  DELETE /history/{session_id} — очистить историю
  POST /admin/ingest            — переиндексировать базу знаний
  GET  /health                  — проверка состояния
"""

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agents.orchestrator import Orchestrator
from config import settings
from knowledge.vector_store import VectorStore
from memory.dialogue_memory import DialogueMemory
from router.intent_router import classify_intent

# ── Startup ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Подключаем LangFuse трассировку (если ключи есть в .env)
    from observability import setup_langfuse
    setup_langfuse()

    # Прогреваем синглтон VectorStore при старте
    store = VectorStore()
    doc_count = store.count()
    print(f"[startup] VectorStore ready. Documents in KB: {doc_count}")
    if doc_count == 0:
        print("[startup] WARNING: Knowledge base is empty. Run: python ingest_data.py")
    yield


app = FastAPI(
    title=f"{settings.company_name} Chatbot API",
    description="AI чат-бот на базе CrewAI с RAG и памятью диалогов",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — разрешаем запросы с сайтов клиентов (виджет)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # В продакшене замени на список конкретных доменов
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# Отдаём виджет как статику: GET /widget/chatbot-widget.js
_widget_dir = Path(__file__).parent / "widget"
if _widget_dir.exists():
    app.mount("/widget", StaticFiles(directory=str(_widget_dir)), name="widget")

# ── Schemas ───────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000, description="Сообщение пользователя")
    session_id: str | None = Field(
        default=None,
        description="ID сессии. Если не передан — создаётся автоматически.",
    )


class ChatResponse(BaseModel):
    response: str
    session_id: str
    intent: str
    used_llm_for_routing: bool


class HistoryMessage(BaseModel):
    role: str
    content: str
    created_at: str


class IngestRequest(BaseModel):
    data_file: str = Field(default="./data/sample_knowledge.json")


# ── Зависимости ───────────────────────────────────────────────────────────────

_orchestrator = Orchestrator()
_memory = DialogueMemory()


# ── Routes ────────────────────────────────────────────────────────────────────


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Основной эндпоинт чата. Принимает сообщение, возвращает ответ агента."""
    session_id = req.session_id or str(uuid.uuid4())
    message = req.message.strip()

    # 1. Получаем историю для контекста роутера
    history = await _memory.get_history(session_id, limit=4)

    # 2. Классифицируем интент (keyword → LLM fallback)
    intent_result = classify_intent(message, history)

    # 3. Запускаем оркестратор
    response = await _orchestrator.process(
        message=message,
        session_id=session_id,
        intent_result=intent_result,
    )

    return ChatResponse(
        response=response,
        session_id=session_id,
        intent=intent_result.intent,
        used_llm_for_routing=intent_result.used_llm,
    )


@app.get("/history/{session_id}", response_model=list[HistoryMessage])
async def get_history(session_id: str):
    """Возвращает историю диалога для указанной сессии."""
    history = await _memory.get_history(session_id)
    if not history:
        raise HTTPException(status_code=404, detail="Session not found or empty")
    return [HistoryMessage(**msg) for msg in history]


@app.delete("/history/{session_id}")
async def clear_history(session_id: str):
    """Очищает историю диалога для сессии."""
    await _memory.clear_session(session_id)
    return {"message": f"History cleared for session {session_id}"}


@app.post("/admin/ingest")
async def ingest_knowledge(req: IngestRequest):
    """
    Переиндексирует базу знаний из JSON файла.
    Вызывай после обновления данных.
    """
    import asyncio
    import subprocess
    import sys

    result = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: subprocess.run(
            [sys.executable, "ingest_data.py", "--file", req.data_file],
            capture_output=True,
            text=True,
        ),
    )

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Ingest failed: {result.stderr}",
        )

    store = VectorStore()
    return {
        "message": "Knowledge base updated",
        "documents_count": store.count(),
        "output": result.stdout,
    }


@app.get("/health")
async def health():
    """Проверка состояния системы."""
    store = VectorStore()
    sessions = await _memory.get_all_sessions()
    return {
        "status": "ok",
        "knowledge_base_docs": store.count(),
        "active_sessions": len(sessions),
        "company": settings.company_name,
        "models": {
            "router": settings.router_model,
            "agent": settings.agent_model,
            "orchestrator": settings.orchestrator_model,
        },
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level="info",
    )
