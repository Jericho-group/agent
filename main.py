"""
FastAPI приложение — основной API чат-бота.

Эндпоинты:
  POST /chat                    — отправить сообщение
  GET  /history/{session_id}   — история сессии
  DELETE /history/{session_id} — очистить историю
  POST /admin/ingest            — переиндексировать базу знаний
  GET  /health                  — проверка состояния
"""

import json
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agents.orchestrator import Orchestrator
from admin_skills_routes import router as skills_router
from config import settings
from knowledge.vector_store import VectorStore
from memory.dialogue_memory import DialogueMemory
from router.intent_router import classify_intent
from proactive import tg_client, campaign_manager

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

    # Telegram-аккаунты грузим В ФОНЕ — MTProto-коннект может ретраить минуты
    # (например при блокировке IPv4 у РКН), а Uvicorn не должен блокироваться.
    tg_client.set_incoming_handler(campaign_manager.handle_incoming)
    import asyncio as _asyncio
    _asyncio.create_task(tg_client.load_active_accounts())
    print("[startup] Proactive TG module scheduled (background)")

    # PRM v1.1: retention worker — раз в час чистит устаревшие события/сессии/soft-deleted
    async def _prm_retention_loop():
        import json as _j_wr
        while True:
            pool = await _b404_pool()
            rid = None
            try:
                rid = await pool.fetchval("INSERT INTO worker_runs (worker) VALUES ('retention') RETURNING id")
                d1 = await pool.execute("DELETE FROM integration_events WHERE created_at < now() - interval '30 days'")
                d2 = await pool.execute("DELETE FROM embed_sessions WHERE expires_at < now() - interval '7 days'")
                d3 = await pool.execute("DELETE FROM async_jobs WHERE updated_at < now() - interval '7 days' AND status IN ('done','failed')")
                d4 = await pool.execute("DELETE FROM tenants WHERE status='pending_deletion' AND delete_scheduled_at IS NOT NULL AND delete_scheduled_at < now()")
                result = {"events": d1, "sessions": d2, "jobs": d3, "tenants": d4}
                await pool.execute("UPDATE worker_runs SET finished_at=now(), ok=true, result=$1::jsonb WHERE id=$2", _j_wr.dumps(result), rid)
                if any(int(x.split()[-1]) for x in (d1, d2, d3, d4) if x and x.split()[-1].isdigit()):
                    print(f"[prm-retention] events={d1} sessions={d2} jobs={d3} tenants={d4}")
            except Exception as e:
                print(f"[prm-retention] err: {e}")
                if rid:
                    try: await pool.execute("UPDATE worker_runs SET finished_at=now(), ok=false, error=$1 WHERE id=$2", str(e)[:500], rid)
                    except: pass
            await _asyncio.sleep(3600)  # раз в час
    _asyncio.create_task(_prm_retention_loop())
    print("[startup] PRM retention worker scheduled (background, hourly)")
    # Task 5: PRM email worker для offline-акторов — раз в 5 мин
    async def _prm_email_loop():
        import json as _j_wr
        while True:
            pool = await _b404_pool()
            rid = None
            try:
                rid = await pool.fetchval("INSERT INTO worker_runs (worker) VALUES ('email') RETURNING id")
                result = await _prm_send_pending_emails()
                await pool.execute("UPDATE worker_runs SET finished_at=now(), ok=true, result=$1::jsonb WHERE id=$2", _j_wr.dumps(result), rid)
            except Exception as e:
                print(f"[prm-email] err: {e}")
                if rid:
                    try: await pool.execute("UPDATE worker_runs SET finished_at=now(), ok=false, error=$1 WHERE id=$2", str(e)[:500], rid)
                    except: pass
            await _asyncio.sleep(300)
    _asyncio.create_task(_prm_email_loop())
    print("[startup] PRM email worker scheduled (background, every 5 min)")
    # Task 9: PRM webhook worker — раз в 30 сек, доставка events на webhook_url integrations
    async def _prm_webhook_loop():
        import json as _j_wr
        while True:
            pool = await _b404_pool()
            rid = None
            try:
                rid = await pool.fetchval("INSERT INTO worker_runs (worker) VALUES ('webhook') RETURNING id")
                result = await _prm_deliver_pending_webhooks()
                await pool.execute("UPDATE worker_runs SET finished_at=now(), ok=true, result=$1::jsonb WHERE id=$2", _j_wr.dumps(result), rid)
            except Exception as e:
                print(f"[prm-webhook] err: {e}")
                if rid:
                    try: await pool.execute("UPDATE worker_runs SET finished_at=now(), ok=false, error=$1 WHERE id=$2", str(e)[:500], rid)
                    except: pass
            await _asyncio.sleep(30)
    _asyncio.create_task(_prm_webhook_loop())
    print("[startup] PRM webhook worker scheduled (background, every 30s)")


    yield


# Sentry / GlitchTip error tracking — включается через SENTRY_DSN env
_SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.asyncpg import AsyncPGIntegration
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            environment=os.environ.get("SENTRY_ENV", "prod"),
            traces_sample_rate=0.0,
            send_default_pii=False,
            integrations=[FastApiIntegration(), AsyncPGIntegration()],
        )
        print(f"[sentry] initialised env={os.environ.get('SENTRY_ENV','prod')}")
    except Exception as _e:
        print(f"[sentry] init failed: {_e}")

app = FastAPI(
    title=f"{settings.company_name} Chatbot API",
    description="AI чат-бот на базе CrewAI с RAG и памятью диалогов",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — разрешаем запросы с сайтов клиентов (виджет)
app.add_middleware(
    CORSMiddleware,
    # Public endpoints (виджет, /chat) — открыты для всех; для /admin/* CORS режется отдельным middleware ниже.
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "PATCH"],
    allow_headers=["*"],
)


# Whitelist для admin-эндпоинтов (бьём в дополнение к CORS-middleware выше).
_ADMIN_ALLOWED_ORIGINS = set(
    o.strip() for o in (os.environ.get("ADMIN_ALLOWED_ORIGINS", "") or "").split(",") if o.strip()
)
_ADMIN_ALLOWED_ORIGINS.update([
    "http://217.149.25.34",
    "https://217.149.25.34",
    "https://217-149-25-34.sslip.io",
    "https://bot.217-149-25-34.sslip.io",
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:8000",
    "http://localhost:8765",
    "http://localhost:8080",
    "http://127.0.0.1:8765",
    "http://127.0.0.1:8080",
    "https://admin.dirizher404.ru",
])


@app.middleware("http")
async def _admin_cors_guard(request: Request, call_next):
    """Для /admin/api/* — реальный CORS-фильтр по Origin. Если запрос пришёл с браузерного
    Origin, который не в whitelist, отвечаем 403. Server-to-server (без Origin) — пропускаем."""
    path = request.url.path or ""
    # /admin/api/embed/* — защищён session-token bearer, разрешаем с любого Origin
    if (path.startswith("/admin/api/") or path.startswith("/admin/ingest")) and not path.startswith("/admin/api/embed/"):
        origin = request.headers.get("origin")
        if origin and origin not in _ADMIN_ALLOWED_ORIGINS:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "origin not allowed"}, status_code=403)
    return await call_next(request)

# ── Security: auth deps определяем до использования в route-декораторах ────
import secrets as _secrets_mod
import time as _time_mod
from memory.dialogue_memory import _get_pool as _b404_pool

try:
    import jwt as _jwt
except ImportError:
    _jwt = None


_INTERNAL_SECRET = getattr(settings, "internal_api_secret", "") or os.environ.get("INTERNAL_API_SECRET") or ""
_JWT_SECRET = os.environ.get("JWT_SECRET") or (settings.admin_token or "")  # fallback на admin_token при бутстрапе
_JWT_ALG = "HS256"
_JWT_TTL_SEC = 24 * 3600  # 24 часа; потом можно перейти на refresh-токены


def issue_jwt(user_id, tenant_id, role: str, scope: str, email: str | None = None) -> str:
    """Выпускает JWT для авторизованного пользователя."""
    if not _jwt:
        raise RuntimeError("PyJWT not installed")
    if not _JWT_SECRET:
        raise RuntimeError("JWT_SECRET not configured")
    now = int(_time_mod.time())
    payload = {
        "sub": str(user_id) if user_id is not None else "",
        "tid": tenant_id,
        "role": role,
        "scope": scope,
        "email": email or "",
        "iat": now,
        "exp": now + _JWT_TTL_SEC,
        "jti": _secrets_mod.token_hex(8),
    }
    return _jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALG)


def _decode_jwt(token: str) -> dict | None:
    if not _jwt or not _JWT_SECRET or not token:
        return None
    try:
        return _jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALG])
    except Exception:
        return None


# Текущий аутентифицированный пользователь (для всех admin/api эндпоинтов).
# Преобразует X-Admin-Token в объект {user_id, tenant_id, role, scope}.
# Поддерживает legacy: старые env-токены (ADMIN_TOKEN, BOT404_TOKEN) — без tenant_id (role=root).
def get_current_user(x_admin_token: str = Header(default="")):
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Missing X-Admin-Token")
    # 1) JWT
    claims = _decode_jwt(x_admin_token)
    if claims:
        return {
            "user_id": int(claims.get("sub")) if claims.get("sub") else None,
            "tenant_id": claims.get("tid"),
            "role": claims.get("role", "viewer"),
            "scope": claims.get("scope", "bot404"),
            "email": claims.get("email", ""),
            "is_legacy": False,
        }
    # 2) Legacy глобальные токены (для server-to-server / bootstrap) — только role=root, без tenant_id
    if settings.admin_token and _secrets_mod.compare_digest(x_admin_token, settings.admin_token):
        return {"user_id": None, "tenant_id": None, "role": "root", "scope": "full", "email": "", "is_legacy": True}
    if settings.bot404_token and _secrets_mod.compare_digest(x_admin_token, settings.bot404_token):
        return {"user_id": None, "tenant_id": None, "role": "root", "scope": "bot404", "email": "", "is_legacy": True}
    raise HTTPException(status_code=401, detail="Invalid or expired token")


def _check_admin(x_admin_token: str = Header(default="")):
    """Legacy: только для root-операций (sysadmin)."""
    expected = settings.admin_token or ""
    if not expected or not _secrets_mod.compare_digest(x_admin_token, expected):
        # Также принимаем JWT с role=root
        claims = _decode_jwt(x_admin_token) if x_admin_token else None
        if not claims or claims.get("role") != "root":
            raise HTTPException(status_code=401, detail="Invalid admin token")


def _check_bot404(x_admin_token: str = Header(default="")):
    # Принимаем JWT либо legacy global-токены. Пускает любую JWT-роль (viewer/member/admin/root).
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Missing X-Admin-Token")
    claims = _decode_jwt(x_admin_token)
    if claims:
        return  # JWT валиден
    if (settings.admin_token and _secrets_mod.compare_digest(x_admin_token, settings.admin_token)) \
       or (settings.bot404_token and _secrets_mod.compare_digest(x_admin_token, settings.bot404_token)):
        return  # Legacy токен
    raise HTTPException(status_code=401, detail="Invalid admin token")


def _check_bot404_admin(x_admin_token: str = Header(default="")):
    """Как _check_bot404, но только для write-операций: role in ('admin','root') или legacy env-токены.
    viewer/member получат 403."""
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Missing X-Admin-Token")
    claims = _decode_jwt(x_admin_token)
    if claims:
        role = claims.get("role", "viewer")
        if role in ("admin", "root"):
            return
        raise HTTPException(status_code=403, detail="Admin role required")
    if (settings.admin_token and _secrets_mod.compare_digest(x_admin_token, settings.admin_token)) \
       or (settings.bot404_token and _secrets_mod.compare_digest(x_admin_token, settings.bot404_token)):
        return  # Legacy env-токены = root
    raise HTTPException(status_code=401, detail="Invalid admin token")


def _check_internal(x_internal_secret: str = Header(default="")):
    if not _INTERNAL_SECRET:
        raise HTTPException(status_code=503, detail="INTERNAL_API_SECRET not configured")
    if not _secrets_mod.compare_digest(x_internal_secret, _INTERNAL_SECRET):
        raise HTTPException(status_code=401, detail="Invalid internal secret")


async def _tenant_id_for(user: dict) -> int | None:
    """Возвращает tenant_id для пользователя. Для legacy-токенов (без tenant_id) делает fallback
    на slug 'aisha' — это переходный период, после полного перехода легаси удалим."""
    if user.get("tenant_id"):
        return user["tenant_id"]
    if user.get("is_legacy"):
        pool = await _b404_pool()
        return await pool.fetchval("SELECT id FROM tenants WHERE slug=$1", "aisha")
    return None

# Отдаём виджет как статику: GET /widget/chatbot-widget.js
_widget_dir = Path(__file__).parent / "widget"
if _widget_dir.exists():
    app.include_router(skills_router)

app.mount("/widget", StaticFiles(directory=str(_widget_dir)), name="widget")

# Task 15: static admin/ файлы (prm-dashboard.html и др. кроме index.html)
_admin_dir = Path(__file__).parent / "admin"
if _admin_dir.exists():
    app.mount("/admin/static", StaticFiles(directory=str(_admin_dir)), name="admin_static")

# PRM iframe SSO v2 removed 2026-08-18 (заменён v1.1 через integrations)

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


@app.get("/history/{session_id}", response_model=list[HistoryMessage], dependencies=[Depends(_check_bot404)])
async def get_history(session_id: str):
    """Возвращает историю диалога для указанной сессии. Требует X-Admin-Token (JWT либо legacy)."""
    history = await _memory.get_history(session_id)
    if not history:
        raise HTTPException(status_code=404, detail="Session not found or empty")
    return [HistoryMessage(**msg) for msg in history]


@app.delete("/history/{session_id}", dependencies=[Depends(_check_bot404_admin)])
async def clear_history(session_id: str):
    """Очищает историю диалога для сессии. Только admin/root."""
    await _memory.clear_session(session_id)
    return {"message": f"History cleared for session {session_id}"}


@app.post("/admin/ingest", dependencies=[Depends(_check_admin)])
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


_STARTED_AT = None  # заполнится на первом обращении к /health


@app.get("/health")
async def health(deep: bool = False):
    """
    Быстрый health-check для uptime-мониторинга.
    ?deep=1 — расширенный (БД, Redis, KB, тенанты). Возвращает 503 при сбое.
    """
    import datetime as _dtmod, time as _time
    global _STARTED_AT
    if _STARTED_AT is None:
        _STARTED_AT = _dtmod.datetime.utcnow()

    result = {"status": "ok", "ts": _dtmod.datetime.utcnow().isoformat() + "Z"}
    checks = {}
    problems = []

    # 1) БД — быстрый SELECT 1
    try:
        t0 = _time.perf_counter()
        pool = await _b404_pool()
        async with pool.acquire() as con:
            await con.fetchval("SELECT 1")
        checks["db"] = {"ok": True, "ms": round((_time.perf_counter() - t0) * 1000, 1)}
    except Exception as e:
        checks["db"] = {"ok": False, "error": str(e)[:200]}
        problems.append("db")

    # 2) Redis — ping
    try:
        t0 = _time.perf_counter()
        r = await _get_redis()
        if r:
            await r.ping()
            checks["redis"] = {"ok": True, "ms": round((_time.perf_counter() - t0) * 1000, 1)}
        else:
            checks["redis"] = {"ok": False, "error": "client not initialised"}
            problems.append("redis")
    except Exception as e:
        checks["redis"] = {"ok": False, "error": str(e)[:200]}
        problems.append("redis")

    result["checks"] = checks
    result["uptime_s"] = int((_dtmod.datetime.utcnow() - _STARTED_AT).total_seconds())

    if deep:
        try:
            store = VectorStore()
            sessions = await _memory.get_all_sessions()
            result["knowledge_base_docs"] = store.count()
            result["active_sessions"] = len(sessions)
            result["company"] = settings.company_name
            result["models"] = {
                "router": settings.router_model,
                "agent": settings.agent_model,
                "orchestrator": settings.orchestrator_model,
            }
            # число тенантов
            try:
                pool = await _b404_pool()
                async with pool.acquire() as con:
                    result["tenants"] = await con.fetchval("SELECT COUNT(*) FROM tenants WHERE enabled=true")
            except Exception:
                pass
        except Exception as e:
            result["deep_error"] = str(e)[:200]

    if problems:
        result["status"] = "degraded"
        # 503 — чтобы uptime-мониторинг сработал
        return JSONResponse(content=result, status_code=503)
    return result


# ── Admin Panel ──────────────────────────────────────────────────────────────
# (_check_admin / _check_internal / _INTERNAL_SECRET определены выше, до маршрутов)



# ═══════════════════════════════════════════════════════════════════════════
# Task 15: PRM Integrations Dashboard — endpoints для твоей админки
# Все требуют _check_bot404_admin (scope='bot404', твой super-scope)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/admin/api/prm/integrations", dependencies=[Depends(_check_bot404_admin)])
async def admin_prm_integrations():
    """Список всех PRM integrations + счётчики."""
    pool = await _b404_pool()
    rows = await pool.fetch("""
        SELECT i.id, i.name, i.external_id, i.status, i.webhook_url IS NOT NULL AS has_webhook,
               i.email_enabled, i.created_at,
               (SELECT COUNT(*) FROM tenants WHERE parent_integration_id=i.id) AS tenants_count,
               (SELECT COUNT(*) FROM integration_events WHERE integration_id=i.id) AS events_count,
               (SELECT MAX(ts) FROM integration_audit WHERE integration_id=i.id) AS last_activity
        FROM integrations i ORDER BY i.id
    """)
    return {"integrations": [dict(r) for r in rows]}


@app.get("/admin/api/prm/integrations/{integration_id}/tenants", dependencies=[Depends(_check_bot404_admin)])
async def admin_prm_tenants(integration_id: int):
    """Список тенантов integration + счётчики."""
    pool = await _b404_pool()
    rows = await pool.fetch("""
        SELECT t.id, t.slug, t.name, t.external_id, t.status, t.created_at,
               (SELECT COUNT(*) FROM bot_404_leads WHERE tenant_id=t.id) AS leads_count,
               (SELECT COUNT(*) FROM tenant_operators WHERE tenant_id=t.id AND active=true) AS ops_count,
               (SELECT COUNT(*) FROM tenant_contacts WHERE tenant_id=t.id AND active=true) AS contacts_count,
               (SELECT COUNT(*) FROM embed_sessions WHERE tenant_id=t.id AND revoked_at IS NULL AND expires_at > now()) AS active_sessions
        FROM tenants t WHERE parent_integration_id=$1 ORDER BY t.id
    """, integration_id)
    return {"tenants": [dict(r) for r in rows]}


@app.get("/admin/api/prm/audit", dependencies=[Depends(_check_bot404_admin)])
async def admin_prm_audit(integration_id: int | None = None, hours: int = 24, only_errors: bool = False, limit: int = 200):
    """Аудит PRM API запросов."""
    pool = await _b404_pool()
    conds = [f"ts > now() - interval '{max(1, min(720, hours))} hours'"]
    params = []
    i = 1
    if integration_id:
        conds.append(f"integration_id = ${i}"); params.append(integration_id); i += 1
    if only_errors:
        conds.append("status_code >= 400")
    params.append(min(500, max(1, limit)))
    rows = await pool.fetch(
        f"SELECT id, integration_id, ts, actor_ip::text as actor_ip, method, path, status_code, payload_masked "
        f"FROM integration_audit WHERE {' AND '.join(conds)} ORDER BY ts DESC LIMIT ${i}",
        *params
    )
    return {"audit": [dict(r) for r in rows], "count": len(rows)}


@app.get("/admin/api/prm/events", dependencies=[Depends(_check_bot404_admin)])
async def admin_prm_events(integration_id: int | None = None, hours: int = 24, limit: int = 200):
    """Список integration_events."""
    pool = await _b404_pool()
    conds = [f"created_at > now() - interval '{max(1, min(720, hours))} hours'"]
    params = []
    i = 1
    if integration_id:
        conds.append(f"integration_id = ${i}"); params.append(integration_id); i += 1
    params.append(min(500, max(1, limit)))
    rows = await pool.fetch(
        f"SELECT id, integration_id, tenant_id, actor_id, actor_type, event_type, payload, "
        f"       webhook_delivered_at, webhook_attempts, email_sent_at, created_at "
        f"FROM integration_events WHERE {' AND '.join(conds)} ORDER BY id DESC LIMIT ${i}",
        *params
    )
    return {"events": [dict(r) for r in rows], "count": len(rows)}


@app.get("/admin/api/prm/worker-runs", dependencies=[Depends(_check_bot404_admin)])
async def admin_prm_worker_runs(worker: str | None = None, hours: int = 24, limit: int = 200):
    """История запусков workers (retention/email/webhook)."""
    pool = await _b404_pool()
    conds = [f"started_at > now() - interval '{max(1, min(720, hours))} hours'"]
    params = []
    i = 1
    if worker:
        conds.append(f"worker = ${i}"); params.append(worker); i += 1
    params.append(min(500, max(1, limit)))
    rows = await pool.fetch(
        f"SELECT id, worker, ok, result, error, started_at, finished_at, "
        f"       EXTRACT(EPOCH FROM (finished_at - started_at))*1000 as duration_ms "
        f"FROM worker_runs WHERE {' AND '.join(conds)} ORDER BY id DESC LIMIT ${i}",
        *params
    )
    return {"runs": [dict(r) for r in rows], "count": len(rows)}


@app.get("/admin/api/prm/sessions", dependencies=[Depends(_check_bot404_admin)])
async def admin_prm_sessions(integration_id: int | None = None, active_only: bool = True, limit: int = 100):
    """Активные embed-сессии PRM (можешь revoke через отдельный endpoint)."""
    pool = await _b404_pool()
    conds = ["1=1"]
    params = []
    i = 1
    if integration_id:
        conds.append(f"t.parent_integration_id = ${i}"); params.append(integration_id); i += 1
    if active_only:
        conds.append("es.revoked_at IS NULL AND es.expires_at > now()")
    params.append(min(500, max(1, limit)))
    rows = await pool.fetch(
        f"SELECT es.code, es.tenant_id, t.slug, es.actor_id, es.actor_type, "
        f"       es.created_at, es.expires_at, es.last_activity_at, es.revoked_at "
        f"FROM embed_sessions es LEFT JOIN tenants t ON t.id = es.tenant_id "
        f"WHERE {' AND '.join(conds)} ORDER BY es.created_at DESC LIMIT ${i}",
        *params
    )
    return {"sessions": [dict(r) for r in rows], "count": len(rows)}

# ── Мульти-аккаунт: логин/пароль -> токен + scope ─────────────────────────────
from pydantic import BaseModel as _BaseModel
# (_b404_pool импортирован выше в auth-секции)


def _accounts():
    return {
        settings.agent_login:  {"password": settings.agent_password,  "token": settings.admin_token,  "scope": "full"},
        settings.acc404_login: {"password": settings.acc404_password, "token": settings.bot404_token, "scope": "bot404"},
    }


class _LoginBody(_BaseModel):
    login: str
    password: str


async def _tenant_id_from_token(x_admin_token: str) -> int | None:
    """Достаёт tenant_id из JWT. Для legacy-токенов делает fallback на slug 'aisha'.
    Возвращает None если не удалось разрешить."""
    if not x_admin_token:
        return None
    claims = _decode_jwt(x_admin_token)
    if claims and claims.get("tid"):
        return int(claims["tid"])
    # legacy fallback: только для глобальных токенов (root-операции / server-to-server)
    if (settings.admin_token and _secrets_mod.compare_digest(x_admin_token, settings.admin_token)) \
       or (settings.bot404_token and _secrets_mod.compare_digest(x_admin_token, settings.bot404_token)):
        pool = await _b404_pool()
        return await pool.fetchval("SELECT id FROM tenants WHERE slug=$1", "aisha")
    return None


async def _tenant_slug_from_token(x_admin_token: str) -> str:
    """Достаёт slug тенанта (для аудит-лога и имён файлов CSV). Fallback 'aisha'."""
    claims = _decode_jwt(x_admin_token) if x_admin_token else None
    if claims and claims.get("tid"):
        pool = await _b404_pool()
        s = await pool.fetchval("SELECT slug FROM tenants WHERE id=$1", int(claims["tid"]))
        if s: return s
    return "aisha"


# Rate-limit /admin/api/login — 10 попыток / 5 минут на IP. Защита от брутфорса.
import time as _time
from collections import deque as _deque
_login_attempts: dict[str, _deque] = {}


def _login_ratelimit(req: Request) -> None:
    ip = req.headers.get("x-forwarded-for", "").split(",")[0].strip() or (req.client.host if req.client else "?")
    now = _time.monotonic()
    window = 300.0
    dq = _login_attempts.setdefault(ip, _deque())
    while dq and dq[0] < now - window:
        dq.popleft()
    if len(dq) >= 10:
        raise HTTPException(status_code=429, detail="too many login attempts, try later")
    dq.append(now)


@app.post("/admin/api/login")
async def admin_login(body: _LoginBody, request: Request):
    _login_ratelimit(request)
    # Phase 10: сначала проверяем БД tenant_users (bcrypt), потом fallback на env
    try:
        import bcrypt as _bcrypt
        pool = await _b404_pool()
        row = await pool.fetchrow(
            """SELECT u.id, u.email, u.role, u.pwd_hash, u.tenant_id, u.enabled, t.slug AS tenant_slug, t.enabled AS tenant_enabled
               FROM tenant_users u
               LEFT JOIN tenants t ON t.id = u.tenant_id
               WHERE u.email=$1 LIMIT 1""",
            body.login,
        )
        if row and row["pwd_hash"] and row["enabled"] and (row["tenant_id"] is None or row["tenant_enabled"]):
            if _bcrypt.checkpw(body.password.encode("utf-8")[:72], row["pwd_hash"].encode("utf-8")):
                await pool.execute("UPDATE tenant_users SET last_login_at=now() WHERE id=$1", row["id"])
                scope = "full" if row["role"] == "root" else "bot404"
                # JWT с tenant_id, role — каждый запрос мы узнаём этого пользователя
                token = issue_jwt(
                    user_id=row["id"], tenant_id=row["tenant_id"],
                    role=row["role"], scope=scope, email=body.login,
                )
                await _audit("user.login", {"user_id": row["id"], "email": body.login, "role": row["role"]},
                             slug=(row["tenant_slug"] or _TENANT_SLUG), actor_email=body.login)
                return {"token": token, "scope": scope, "login": body.login, "role": row["role"],
                        "tenant_slug": row["tenant_slug"]}
    except Exception as e:
        print(f"[login] db check fail: {e}")
    # Fallback: env-логины (для bootstrap-сценариев). Пустой пароль ВСЕГДА fail.
    acc = _accounts().get(body.login)
    if not acc or not acc["password"] or not body.password or acc["password"] != body.password:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    return {"token": acc["token"], "scope": acc["scope"], "login": body.login}


# ── Команда: список / добавить / удалить юзера тенанта ──────────────────────
@app.get("/admin/api/team", dependencies=[Depends(_check_bot404)])
async def team_list(x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    rows = await pool.fetch(
        """SELECT id, email, role, enabled, last_login_at, created_at FROM tenant_users
           WHERE tenant_id=$1 ORDER BY id""",
        tid,
    )
    return {"users": [dict(r) for r in rows]}


class _AddUserBody(_BaseModel):
    email: str
    password: str
    role: str = "member"


@app.post("/admin/api/team", dependencies=[Depends(_check_bot404_admin)])
async def team_add(body: _AddUserBody, x_admin_token: str = Header(default="")):
    import bcrypt as _bcrypt
    if not body.email or not body.password or len(body.password) < 8:
        raise HTTPException(status_code=400, detail="email обязателен, пароль не короче 8 символов")
    if body.role not in ("admin", "member", "viewer"):
        raise HTTPException(status_code=400, detail="роль: admin / member / viewer")
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    if tid is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    h = _bcrypt.hashpw(body.password.encode("utf-8")[:72], _bcrypt.gensalt()).decode("utf-8")
    try:
        uid = await pool.fetchval(
            "INSERT INTO tenant_users(tenant_id, email, pwd_hash, role) VALUES($1,$2,$3,$4) RETURNING id",
            tid, body.email, h, body.role,
        )
    except Exception as e:
        if "duplicate" in str(e).lower():
            raise HTTPException(status_code=409, detail="Такой email уже есть в команде")
        raise
    await _audit("team.add", {"id": uid, "email": body.email, "role": body.role}, tid=tid)
    return {"ok": True, "id": uid}


@app.delete("/admin/api/team/{user_id}", dependencies=[Depends(_check_bot404_admin)])
async def team_delete(user_id: int, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    deleted = await pool.fetchval(
        """DELETE FROM tenant_users u
           WHERE u.id=$1 AND u.tenant_id=$2 AND u.role != 'root'
           RETURNING u.id""",
        user_id, tid,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="not found or protected")
    await _audit("team.delete", {"id": user_id}, tid=tid)
    return {"ok": True}


@app.get("/admin/api/bot404/dialogs", dependencies=[Depends(_check_bot404)])
async def bot404_dialogs(x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    rows = await pool.fetch(
        """SELECT l.session_id,
                  count(*)::int AS msgs,
                  min(l.created_at) AS started,
                  max(l.created_at) AS last_at,
                  (SELECT text FROM bot_404_log x WHERE x.session_id=l.session_id AND x.tenant_id=$1 ORDER BY x.id DESC LIMIT 1) AS last_text,
                  EXISTS(SELECT 1 FROM bot_404_leads d WHERE d.session_id=l.session_id AND d.tenant_id=$1) AS has_lead,
                  COALESCE(
                    (SELECT '@' || u.username FROM bot_404_tg_users u WHERE u.session_id = l.session_id AND u.username IS NOT NULL LIMIT 1),
                    (SELECT telegram FROM bot_404_leads d WHERE d.session_id = l.session_id AND d.telegram IS NOT NULL AND d.tenant_id=$1 LIMIT 1)
                  ) AS tg_username,
                  COALESCE(
                    (SELECT u.first_name FROM bot_404_tg_users u WHERE u.session_id = l.session_id AND u.first_name IS NOT NULL LIMIT 1),
                    (SELECT split_part(replace(note, 'Telegram: ', ''), chr(10), 1)
                       FROM bot_404_leads d WHERE d.session_id = l.session_id AND d.note LIKE 'Telegram: %' AND d.tenant_id=$1 LIMIT 1)
                  ) AS tg_name,
                  CASE WHEN l.session_id LIKE 'tg:%' THEN reverse(split_part(reverse(l.session_id), ':', 1)) ELSE NULL END AS tg_chat_id,
                  COALESCE((SELECT human_takeover FROM session_meta sm WHERE sm.session_id=l.session_id), false) AS human_takeover,
                  COALESCE((SELECT escalated FROM session_meta sm WHERE sm.session_id=l.session_id), false) AS escalated
           FROM bot_404_log l
           WHERE l.tenant_id=$1
           GROUP BY l.session_id
           ORDER BY last_at DESC LIMIT 200""", tid,
    )
    return {"dialogs": [dict(r) for r in rows]}


@app.get("/admin/api/bot404/clients", dependencies=[Depends(_check_bot404)])
async def bot404_clients(x_admin_token: str = Header(default="")):
    """
    Клиент-центричный вид: группирует сессии по нормализованному контакту
    (телефон цифрами / email lowercase / @telegram lowercase).
    Сессии без контакта возвращаются как отдельные «анонимные» клиенты.
    """
    import re
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)

    # 1) Все сессии тенанта + базовая мета
    sess_rows = await pool.fetch(
        """SELECT l.session_id,
                  count(*)::int AS msgs,
                  max(l.created_at) AS last_at,
                  (SELECT text FROM bot_404_log x WHERE x.session_id=l.session_id AND x.tenant_id=$1 ORDER BY x.id DESC LIMIT 1) AS last_text
             FROM bot_404_log l
            WHERE l.tenant_id=$1
            GROUP BY l.session_id
            ORDER BY last_at DESC LIMIT 1000""", tid)

    # 2) Лиды (phone/email/telegram) для всех этих сессий
    lead_rows = await pool.fetch(
        "SELECT session_id, phone, email, telegram, name FROM bot_404_leads WHERE tenant_id=$1", tid)
    leads_by_sid: dict[str, dict] = {}
    for r in lead_rows:
        sid = r["session_id"]
        cur = leads_by_sid.setdefault(sid, {"phone": None, "email": None, "telegram": None, "name": None})
        for k in ("phone", "email", "telegram", "name"):
            if r[k] and not cur[k]:
                cur[k] = r[k]

    # 3) TG-username для сессий tg:* (часто контакт прячется именно тут)
    tg_rows = await pool.fetch(
        "SELECT session_id, username, first_name FROM bot_404_tg_users WHERE tenant_id=$1", tid)
    tg_by_sid: dict[str, dict] = {r["session_id"]: {"username": r["username"], "first_name": r["first_name"]} for r in tg_rows}

    # 4) Facts (industry / last_summary) по сессиям
    facts_rows = await pool.fetch(
        "SELECT session_id, industry, last_summary FROM bot_404_session_facts WHERE tenant_id=$1", tid)
    facts_by_sid: dict[str, dict] = {r["session_id"]: dict(r) for r in facts_rows}

    def norm_phone(p: str | None) -> str | None:
        if not p: return None
        d = re.sub(r"\D", "", p)
        return d or None

    def norm_tg(t: str | None) -> str | None:
        if not t: return None
        return t.lstrip("@").lower() or None

    # 5) Группировка
    clients: dict[str, dict] = {}
    for s in sess_rows:
        sid = s["session_id"]
        lead = leads_by_sid.get(sid) or {}
        tg = tg_by_sid.get(sid) or {}
        phone = norm_phone(lead.get("phone"))
        email = (lead.get("email") or "").lower() or None
        # TG: сначала из лида (если клиент сам прислал @username), потом из bot_404_tg_users
        telegram = norm_tg(lead.get("telegram")) or norm_tg(tg.get("username"))

        # Ключ группировки: phone > email > telegram > сам session_id (анонимный)
        if phone:
            key, ctype, cvalue = f"phone:{phone}", "phone", phone
        elif email:
            key, ctype, cvalue = f"email:{email}", "email", email
        elif telegram:
            key, ctype, cvalue = f"tg:{telegram}", "telegram", "@" + telegram
        else:
            key, ctype, cvalue = f"anon:{sid}", "anon", sid

        c = clients.setdefault(key, {
            "contact_key": key,
            "contact_type": ctype,
            "contact_value": cvalue,
            "display_name": lead.get("name") or tg.get("first_name") or "",
            "sessions": [],
            "total_msgs": 0,
            "last_at": None,
            "has_lead": False,
            "industry": None,
            "last_summary": None,
        })
        if not c["display_name"]:
            c["display_name"] = lead.get("name") or tg.get("first_name") or ""
        if lead.get("phone") or lead.get("email") or lead.get("telegram"):
            c["has_lead"] = True
        f = facts_by_sid.get(sid)
        if f:
            if f.get("industry") and not c["industry"]:
                c["industry"] = f["industry"]
            if f.get("last_summary") and not c["last_summary"]:
                c["last_summary"] = f["last_summary"]
        c["sessions"].append({
            "session_id": sid,
            "msgs": s["msgs"],
            "last_at": s["last_at"].isoformat() if s["last_at"] else None,
            "last_text": (s["last_text"] or "")[:140],
        })
        c["total_msgs"] += s["msgs"]
        if not c["last_at"] or (s["last_at"] and s["last_at"].isoformat() > c["last_at"]):
            c["last_at"] = s["last_at"].isoformat() if s["last_at"] else None

    # 6) Сортировка: сначала клиенты с лидом, затем по дате
    result = sorted(clients.values(), key=lambda c: (not c["has_lead"], c["last_at"] or ""), reverse=True)
    # Внутри клиента — сессии по дате
    for c in result:
        c["sessions"].sort(key=lambda s: s["last_at"] or "", reverse=True)
        c["session_count"] = len(c["sessions"])
    return {"clients": result}


class AudienceSegmentReq(BaseModel):
    filter: str
    product: str | None = None
    industry: str | None = None


@app.post("/admin/api/bot404/audience-segment", dependencies=[Depends(_check_bot404)])
async def bot404_audience_segment(req: AudienceSegmentReq, x_admin_token: str = Header(default="")):
    """
    Возвращает список контактов для рассылки из тех, кто уже писал боту.
    Только TG-пользователи (с @username) — для рассылки через реальный TG-профиль.
    """
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)

    # Базовый SELECT — TG-юзеры этого тенанта + последняя активность
    base_query = """
        SELECT DISTINCT ON (u.session_id)
               u.session_id,
               '@' || u.username AS username,
               u.first_name AS name,
               u.last_seen_at AS last_seen,
               (SELECT text FROM bot_404_log l
                WHERE l.session_id=u.session_id AND l.tenant_id=$1
                ORDER BY l.id DESC LIMIT 1) AS last_message,
               f.industry, f.product_interest, f.demo_promised,
               f.contact_email, f.contact_phone, f.contact_telegram
        FROM bot_404_tg_users u
        LEFT JOIN bot_404_session_facts f ON f.session_id=u.session_id
        WHERE u.tenant_id=$1 AND u.username IS NOT NULL AND u.username <> ''
    """

    now_expr = "now()"
    filter_ = (req.filter or "").strip()
    params: list = [tid]
    where: list[str] = []

    if filter_ == "active_yesterday":
        # был активен вчера (>24ч но <48ч назад ИЛИ вчерашний день)
        where.append(f"u.last_seen_at::date = ({now_expr} - INTERVAL '1 day')::date")
    elif filter_ == "active_7d":
        where.append(f"u.last_seen_at > {now_expr} - INTERVAL '7 days'")
    elif filter_ == "active_30d":
        where.append(f"u.last_seen_at > {now_expr} - INTERVAL '30 days'")
    elif filter_ == "inactive_30d":
        where.append(f"(u.last_seen_at IS NULL OR u.last_seen_at < {now_expr} - INTERVAL '30 days')")
    elif filter_ == "unfinished_contact":
        # оставил контакт, но диалог закончился <10 сообщений (пример эвристики)
        where.append("(f.contact_email IS NOT NULL OR f.contact_phone IS NOT NULL OR f.contact_telegram IS NOT NULL)")
        where.append("(SELECT count(*) FROM bot_404_log l WHERE l.session_id=u.session_id) < 10")
    elif filter_ == "product_interest":
        if not req.product:
            raise HTTPException(400, "product обязателен для этого фильтра")
        params.append(req.product.lower())
        where.append(f"lower(f.product_interest) = ${len(params)}")
    elif filter_ == "industry":
        if not req.industry:
            raise HTTPException(400, "industry обязателен для этого фильтра")
        params.append(req.industry.lower())
        where.append(f"lower(f.industry) = ${len(params)}")
    elif filter_ == "demo_no_show":
        # обещали демо, но с тех пор >3 дней тишины
        where.append("f.demo_promised = true")
        where.append(f"(u.last_seen_at IS NULL OR u.last_seen_at < {now_expr} - INTERVAL '3 days')")
    else:
        raise HTTPException(400, f"неизвестный фильтр: {filter_}")

    query = base_query + " AND " + " AND ".join(where) + " ORDER BY u.session_id, u.last_seen_at DESC NULLS LAST LIMIT 500"
    rows = await pool.fetch(query, *params)

    audience = []
    for r in rows:
        audience.append({
            "session_id": r["session_id"],
            "username": r["username"],
            "name": r["name"] or "",
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            "last_message": (r["last_message"] or "")[:200],
            "industry": r["industry"],
            "product_interest": r["product_interest"],
        })
    return {"count": len(audience), "audience": audience, "filter": filter_}


class SegmentImportReq(BaseModel):
    filter: str
    product: str | None = None
    industry: str | None = None


@app.post("/admin/api/proactive/campaigns/{campaign_id}/import-segment", dependencies=[Depends(_check_bot404)])
async def proactive_import_segment(campaign_id: int, req: SegmentImportReq, x_admin_token: str = Header(default="")):
    """Одним запросом: посчитать сегмент и сразу залить в кампанию как контактов."""
    tid = await _tenant_id_from_token(x_admin_token)
    seg = await bot404_audience_segment(
        AudienceSegmentReq(filter=req.filter, product=req.product, industry=req.industry),
        x_admin_token,
    )
    contacts = [{"username": a["username"], "name": a.get("name") or None} for a in seg.get("audience", [])]
    try:
        added = await campaign_manager.add_contacts(campaign_id, contacts, tid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"segment_size": seg["count"], "added": added, "filter": req.filter}


@app.get("/admin/api/bot404/transcript", dependencies=[Depends(_check_bot404)])
async def bot404_transcript(sid: str, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    rows = await pool.fetch("SELECT direction, text, created_at FROM bot_404_log WHERE session_id=$1 AND tenant_id=$2 ORDER BY id ASC LIMIT 500", sid, tid)
    return {"messages": [dict(r) for r in rows]}


@app.get("/admin/api/bot404/leads", dependencies=[Depends(_check_bot404)])
async def bot404_leads(x_admin_token: str = Header(default=""), status: str | None = None, search: str | None = None):
    """Список лидов тенанта с обогащением из session_facts (industry/чек/volume)."""
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    where = ["l.tenant_id=$1"]
    params: list = [tid]
    if status:
        params.append(status)
        where.append(f"l.status = ${len(params)}")
    if search:
        params.append(f"%{search}%")
        idx = len(params)
        where.append(f"(l.name ILIKE ${idx} OR l.phone ILIKE ${idx} OR l.email ILIKE ${idx} OR l.telegram ILIKE ${idx} OR l.company ILIKE ${idx})")
    rows = await pool.fetch(
        f"""SELECT l.id, l.session_id, l.name, l.phone, l.email, l.telegram, l.company, l.note,
                   l.created_at, l.updated_at, l.status,
                   COALESCE(l.industry, f.industry) AS industry,
                   COALESCE(l.avg_check_rub, f.avg_check_rub) AS avg_check_rub,
                   l.manager_notes,
                   f.volume_per_day, f.product_interest, f.current_crm,
                   CASE
                     WHEN l.session_id LIKE 'tg:%%' THEN 'telegram'
                     ELSE 'widget'
                   END AS channel
              FROM bot_404_leads l
              LEFT JOIN bot_404_session_facts f ON f.session_id = l.session_id
             WHERE {" AND ".join(where)}
             ORDER BY l.updated_at DESC NULLS LAST, l.created_at DESC
             LIMIT 500""",
        *params,
    )
    return {"leads": [dict(r) for r in rows]}


class _LeadPatch(BaseModel):
    status: str | None = None
    manager_notes: str | None = None
    industry: str | None = None
    avg_check_rub: float | None = None


@app.patch("/admin/api/bot404/leads/{lead_id}", dependencies=[Depends(_check_bot404)])
async def bot404_lead_patch(lead_id: int, req: _LeadPatch, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    updates = []
    params: list = []
    if req.status is not None:
        if req.status not in ("new", "in_work", "called", "pilot", "client", "refused"):
            raise HTTPException(400, "invalid status")
        params.append(req.status); updates.append(f"status = ${len(params)}")
    if req.manager_notes is not None:
        params.append(req.manager_notes); updates.append(f"manager_notes = ${len(params)}")
    if req.industry is not None:
        params.append(req.industry); updates.append(f"industry = ${len(params)}")
    if req.avg_check_rub is not None:
        params.append(req.avg_check_rub); updates.append(f"avg_check_rub = ${len(params)}")
    if not updates:
        raise HTTPException(400, "nothing to update")
    updates.append("updated_at = now()")
    params.append(lead_id); params.append(tid)
    q = f"UPDATE bot_404_leads SET {', '.join(updates)} WHERE id = ${len(params)-1} AND tenant_id = ${len(params)} RETURNING id, status, manager_notes, updated_at"
    row = await pool.fetchrow(q, *params)
    if not row:
        raise HTTPException(404, "lead not found or not owned")
    return dict(row)


@app.get("/admin/api/bot404/leads/stats", dependencies=[Depends(_check_bot404)])
async def bot404_leads_stats(x_admin_token: str = Header(default="")):
    """Счётчики по статусам для табов/бейджей. Регистрируется ДО /{lead_id} чтобы не поглощался path-параметром."""
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    rows = await pool.fetch("SELECT status, count(*)::int AS n FROM bot_404_leads WHERE tenant_id=$1 GROUP BY status", tid)
    return {"by_status": {r["status"]: r["n"] for r in rows}}


@app.get("/admin/api/bot404/leads/{lead_id}", dependencies=[Depends(_check_bot404)])
async def bot404_lead_detail(lead_id: int, x_admin_token: str = Header(default="")):
    """Детали лида + связанный диалог (последние 30 сообщений)."""
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    lead = await pool.fetchrow(
        """SELECT l.*, f.industry AS fact_industry, f.volume_per_day, f.avg_check_rub AS fact_avg_check,
                  f.product_interest, f.current_crm, f.mentioned_pains, f.last_summary
             FROM bot_404_leads l
             LEFT JOIN bot_404_session_facts f ON f.session_id = l.session_id
            WHERE l.id=$1 AND l.tenant_id=$2""",
        lead_id, tid,
    )
    if not lead:
        raise HTTPException(404, "not found")
    messages = await pool.fetch(
        "SELECT direction, text, created_at FROM bot_404_log WHERE session_id=$1 AND tenant_id=$2 ORDER BY id DESC LIMIT 30",
        lead["session_id"], tid,
    )
    return {"lead": dict(lead), "messages": [dict(m) for m in reversed(messages)]}



@app.get("/admin/api/bot404/stats", dependencies=[Depends(_check_bot404)])
async def bot404_stats(x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    sessions = await pool.fetchval("SELECT count(DISTINCT session_id) FROM bot_404_log WHERE tenant_id=$1", tid) or 0
    total = await pool.fetchval("SELECT count(*) FROM bot_404_log WHERE tenant_id=$1", tid) or 0
    last24 = await pool.fetchval("SELECT count(*) FROM bot_404_log WHERE tenant_id=$1 AND created_at > now() - interval '24 hours'", tid) or 0
    leads = await pool.fetchval("SELECT count(*) FROM bot_404_leads WHERE tenant_id=$1", tid) or 0
    kb_docs = await pool.fetchval("SELECT count(*) FROM knowledge_base WHERE tenant_id=$1", tid) or 0
    # company — из брендинга тенанта, а не хардкод 404ai (иначе клиент видит вендора в своём кабинете)
    brand = await pool.fetchval("SELECT brand_name FROM v_tenant_branding WHERE tenant_id=$1", tid) or ""
    bot_nm = await pool.fetchval("SELECT bot_name FROM v_tenant_branding WHERE tenant_id=$1", tid) or ""
    company = (str(brand) + (" · " + str(bot_nm) if bot_nm else "")).strip(" ·") or "—"
    # модель — эффективная модель тенанта (а не хардкод вендора); эскалации — реальный счётчик
    tmodel = await pool.fetchval("SELECT model FROM v_tenant_effective_limits WHERE tenant_id=$1", tid) or "gemini-2.5-flash-lite"
    escalated = await pool.fetchval("SELECT count(*) FROM session_meta WHERE tenant_id=$1 AND escalated", tid) or 0
    return {
        "stats": {"status": "online", "active_sessions": sessions, "knowledge_base_docs": kb_docs,
                  "models": {"agent": tmodel, "router": tmodel, "orchestrator": tmodel},
                  "company": company},
        "extStats": {"total_sessions": sessions, "total_messages": total, "messages_24h": last24,
                     "qualified_leads": leads, "escalated": escalated, "temperature": {"hot": 0, "warm": 0, "cold": 0}, "intents": []},
    }



@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(Path(__file__).parent / "admin" / "favicon.svg", media_type="image/svg+xml")


@app.get("/admin")
async def admin_panel():
    """Веб-интерфейс управления."""
    return FileResponse(
        Path(__file__).parent / "admin" / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/admin/api/sessions", dependencies=[Depends(_check_bot404)])
async def admin_sessions(x_admin_token: str = Header(default="")):
    """Список сессий ТЕКУЩЕГО ТЕНАНТА с количеством сообщений."""
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    rows = await pool.fetch(
        """SELECT session_id, COUNT(*)::int AS message_count
           FROM bot_404_log WHERE tenant_id=$1
           GROUP BY session_id ORDER BY MAX(created_at) DESC LIMIT 500""",
        tid,
    )
    return [{"session_id": r["session_id"], "message_count": r["message_count"]} for r in rows]


@app.post("/admin/api/knowledge/upload", dependencies=[Depends(_check_bot404_admin)])
async def upload_knowledge(file: UploadFile = File(...), x_admin_token: str = Header(default="")):
    """Загрузить JSON файл базы знаний — статьи прикрепляются к ТЕКУЩЕМУ ТЕНАНТУ."""
    tid = await _tenant_id_from_token(x_admin_token)
    if not tid:
        raise HTTPException(status_code=401, detail="tenant not resolved")
    content = await file.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    # upsert каждой записи с tenant_id текущего юзера
    import os as _os
    from openai import OpenAI as _OpenAI
    client = _OpenAI(api_key=_os.environ.get("OPENAI_API_KEY"), base_url=_os.environ.get("OPENAI_BASE_URL") or None)
    store = VectorStore()
    from knowledge.vector_store import _get_conn as _kb_conn
    conn = _kb_conn(); conn.autocommit = True
    inserted = 0
    for item in data:
        # Изоляция тенантов: id — глобальный текстовый PK. Без префикса одинаковый
        # item.id у двух тенантов (напр. "about_001") затрёт чужую статью через ON CONFLICT.
        raw_id = str(item.get("id") or f"upload-{tid}-{inserted}")
        doc_id = raw_id if raw_id.startswith(f"{tid}::") else f"{tid}::{raw_id}"
        text = item.get("content") or item.get("text") or ""
        if not text: continue
        emb_resp = client.embeddings.create(model=_os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"), input=text)
        emb = "[" + ",".join(str(x) for x in emb_resp.data[0].embedding) + "]"
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO knowledge_base (id, content, embedding, category, title, source, tenant_id)
                   VALUES (%s, %s, %s::vector, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET
                     content=EXCLUDED.content, embedding=EXCLUDED.embedding,
                     category=EXCLUDED.category, title=EXCLUDED.title, source=EXCLUDED.source,
                     tenant_id=EXCLUDED.tenant_id, updated_at=now()
                   WHERE knowledge_base.tenant_id = EXCLUDED.tenant_id""",
                (doc_id, text, emb, item.get("category"), item.get("title"), item.get("source"), tid),
            )
        inserted += 1
    return {"uploaded": inserted, "total": store.count()}


@app.get("/admin/api/knowledge/list", dependencies=[Depends(_check_bot404)])
async def list_knowledge(category: str | None = None, search: str | None = None, x_admin_token: str = Header(default="")):
    """Список документов KB ТЕКУЩЕГО ТЕНАНТА для админки."""
    tid = await _tenant_id_from_token(x_admin_token)
    from knowledge.vector_store import _get_conn as _kb_conn
    import psycopg2.extras as _pgx
    conn = _kb_conn()
    # Изоляция тенантов: показываем ТОЛЬКО статьи своего тенанта (как и search_knowledge).
    # Раньше был "OR tenant_id IS NULL" — легаси-строки без владельца светились бы всем.
    conditions = ["tenant_id = %s"]
    params = [tid]
    if category:
        conditions.append("category = %s"); params.append(category)
    if search:
        conditions.append("(title ILIKE %s OR content ILIKE %s)"); params.extend([f"%{search}%", f"%{search}%"])
    where = " AND ".join(conditions)
    params.append(200)
    with conn.cursor(cursor_factory=_pgx.RealDictCursor) as cur:
        cur.execute(
            f"SELECT id, title, category, content, to_char(updated_at, 'YYYY-MM-DD HH24:MI') AS updated_at "
            f"FROM knowledge_base WHERE {where} ORDER BY category, title LIMIT %s",
            params,
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


@app.delete("/admin/api/knowledge", dependencies=[Depends(_check_bot404_admin)])
async def clear_knowledge(x_admin_token: str = Header(default="")):
    """Очистить базу знаний ТЕКУЩЕГО ТЕНАНТА (статьи без tenant_id остаются)."""
    tid = await _tenant_id_from_token(x_admin_token)
    if not tid:
        raise HTTPException(status_code=401, detail="tenant not resolved")
    from knowledge.vector_store import _get_conn as _kb_conn
    conn = _kb_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM knowledge_base WHERE tenant_id=%s", (tid,))
        deleted = cur.rowcount
    return {"message": f"Deleted {deleted} documents for current tenant"}


@app.post("/admin/api/correct", dependencies=[Depends(_check_bot404)])
async def save_correction(data: dict, x_admin_token: str = Header(default="")):
    """Сохранить исправление ответа (few-shot обучение) для ТЕКУЩЕГО ТЕНАНТА."""
    tid = await _tenant_id_from_token(x_admin_token)
    await _memory.save_correction(
        intent=data.get("intent", "unknown"),
        user_msg=data.get("user_msg", ""),
        bad_answer=data.get("bad_answer", ""),
        good_answer=data.get("good_answer", ""),
        tenant_id=tid,
    )
    return {"message": "Correction saved"}


# ── Proactive / Telegram ─────────────────────────────────────────────────────


class TgCodeRequest(BaseModel):
    phone: str

class TgConfirmRequest(BaseModel):
    phone: str
    code: str
    phone_code_hash: str

class CampaignCreate(BaseModel):
    name: str
    account_id: int | None = None
    first_message: str
    goal: str | None = None
    delay_min: int = 60
    delay_max: int = 180

class ContactsUpload(BaseModel):
    contacts: list[dict]  # [{username, phone, name}]


@app.get("/admin/api/proactive/accounts", dependencies=[Depends(_check_bot404)])
async def proactive_accounts(x_admin_token: str = Header(default="")):
    tid = await _tenant_id_from_token(x_admin_token)
    return await tg_client.get_accounts_status(tid)


@app.post("/admin/api/proactive/accounts/request-code", dependencies=[Depends(_check_bot404_admin)])
async def proactive_request_code(req: TgCodeRequest):
    try:
        result = await tg_client.request_code(req.phone)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/admin/api/proactive/accounts/confirm-code", dependencies=[Depends(_check_bot404_admin)])
async def proactive_confirm_code(req: TgConfirmRequest, x_admin_token: str = Header(default="")):
    try:
        tid = await _tenant_id_from_token(x_admin_token)
        await tg_client.confirm_code(req.phone, req.code, req.phone_code_hash, tid)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/admin/api/proactive/accounts/{account_id}", dependencies=[Depends(_check_bot404_admin)])
async def proactive_delete_account(account_id: int, x_admin_token: str = Header(default="")):
    """Удаляет TG-аккаунт из проактивности: останавливает MTProto-клиент,
    удаляет запись из tg_accounts. Кампании с этим account_id остаются, но
    рассылка/приём через этот аккаунт больше невозможны (FK в campaigns).
    """
    tid = await _tenant_id_from_token(x_admin_token)
    if tid is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    pool = await _b404_pool()
    row = await pool.fetchrow(
        "SELECT id, phone, tenant_id FROM tg_accounts WHERE id=$1", account_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="account not found")
    if int(row["tenant_id"]) != int(tid):
        raise HTTPException(status_code=403, detail="not your account")
    phone = row["phone"]
    # Останавливаем MTProto клиент (best-effort)
    try:
        await tg_client.stop_client(phone)
    except Exception as e:
        print(f"[proactive.delete] stop_client fail: {e}")
    # Есть ли кампании с этим аккаунтом? Если да — не даём удалить, надо сначала снести кампании
    used = await pool.fetchval("SELECT count(*) FROM campaigns WHERE account_id=$1", account_id)
    if used and used > 0:
        raise HTTPException(status_code=409, detail=f"У аккаунта {used} привязанных кампаний — сначала удалите их")
    await pool.execute("DELETE FROM tg_accounts WHERE id=$1", account_id)
    await _audit("proactive.account.delete", {"account_id": account_id, "phone": phone}, tid=tid)
    return {"ok": True}


@app.get("/admin/api/proactive/campaigns", dependencies=[Depends(_check_bot404)])
async def proactive_list_campaigns(x_admin_token: str = Header(default="")):
    tid = await _tenant_id_from_token(x_admin_token)
    return await campaign_manager.list_campaigns(tid)


@app.post("/admin/api/proactive/campaigns", dependencies=[Depends(_check_bot404_admin)])
async def proactive_create_campaign(req: CampaignCreate, x_admin_token: str = Header(default="")):
    tid = await _tenant_id_from_token(x_admin_token)
    try:
        return await campaign_manager.create_campaign(req.model_dump(), tid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/admin/api/proactive/campaigns/{campaign_id}", dependencies=[Depends(_check_bot404_admin)])
async def proactive_delete_campaign(campaign_id: int, x_admin_token: str = Header(default="")):
    tid = await _tenant_id_from_token(x_admin_token)
    await campaign_manager.delete_campaign(campaign_id, tid)
    return {"status": "deleted"}


@app.post("/admin/api/proactive/campaigns/{campaign_id}/start", dependencies=[Depends(_check_bot404)])
async def proactive_start_campaign(campaign_id: int, reset: bool = False, x_admin_token: str = Header(default="")):
    tid = await _tenant_id_from_token(x_admin_token)
    try:
        await campaign_manager.start_campaign(campaign_id, tid, reset=reset)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "started"}


@app.post("/admin/api/proactive/campaigns/{campaign_id}/restart", dependencies=[Depends(_check_bot404)])
async def proactive_restart_campaign(campaign_id: int, x_admin_token: str = Header(default="")):
    tid = await _tenant_id_from_token(x_admin_token)
    try:
        await campaign_manager.start_campaign(campaign_id, tid, reset=True)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "restarted"}


@app.post("/admin/api/proactive/campaigns/{campaign_id}/pause", dependencies=[Depends(_check_bot404)])
async def proactive_pause_campaign(campaign_id: int, x_admin_token: str = Header(default="")):
    tid = await _tenant_id_from_token(x_admin_token)
    await campaign_manager.update_campaign_status(campaign_id, "paused", tid)
    return {"status": "paused"}


@app.get("/admin/api/proactive/campaigns/{campaign_id}/contacts", dependencies=[Depends(_check_bot404)])
async def proactive_list_contacts(campaign_id: int, x_admin_token: str = Header(default="")):
    tid = await _tenant_id_from_token(x_admin_token)
    return await campaign_manager.list_contacts(campaign_id, tid)


@app.post("/admin/api/proactive/campaigns/{campaign_id}/contacts", dependencies=[Depends(_check_bot404)])
async def proactive_add_contacts(campaign_id: int, req: ContactsUpload, x_admin_token: str = Header(default="")):
    tid = await _tenant_id_from_token(x_admin_token)
    try:
        added = await campaign_manager.add_contacts(campaign_id, req.contacts, tid)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"added": added}


@app.get("/admin/api/proactive/conversations", dependencies=[Depends(_check_bot404)])
async def proactive_conversations(x_admin_token: str = Header(default=""), campaign_id: int | None = None):
    tid = await _tenant_id_from_token(x_admin_token)
    return await campaign_manager.list_conversations(tid, campaign_id)


@app.get("/admin/api/proactive/conversations/{contact_id}", dependencies=[Depends(_check_bot404)])
async def proactive_conversation_messages(contact_id: int, x_admin_token: str = Header(default="")):
    tid = await _tenant_id_from_token(x_admin_token)
    return await campaign_manager.get_conversation_messages(contact_id, tid)


class _ProTakeoverBody(_BaseModel):
    enabled: bool


@app.post("/admin/api/proactive/conversations/{contact_id}/takeover", dependencies=[Depends(_check_bot404)])
async def proactive_takeover(contact_id: int, body: _ProTakeoverBody, x_admin_token: str = Header(default="")):
    """Оператор берёт/отдаёт управление проактивным диалогом.

    session_id формат `proactive-tg:{contact_id}` — тот же, что использует
    campaign_manager.handle_incoming. При enabled=true бот молчит на входящие,
    ответы уходят через /admin/api/proactive/conversations/{contact_id}/reply.
    """
    tid = await _tenant_id_from_token(x_admin_token)
    if tid is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    pool = await _b404_pool()
    # Проверяем что contact_id принадлежит этому тенанту (через campaign),
    # заодно достаём фактический session_id — тот же, который использует
    # campaign_manager.handle_incoming (contact.session_id — UUID первой рассылки
    # или proactive-tg:{id} fallback). Иначе takeover пишется в session_meta по
    # одному ключу, а /api/sales-chat читает по другому — бот не молчит.
    row = await pool.fetchrow(
        "SELECT c.tenant_id, cc.session_id FROM campaign_contacts cc JOIN campaigns c ON c.id = cc.campaign_id WHERE cc.id=$1",
        contact_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="contact not found")
    if int(row["tenant_id"]) != int(tid):
        raise HTTPException(status_code=403, detail="not your contact")
    sid = row["session_id"] or f"proactive-tg:{contact_id}"
    await pool.execute(
        """INSERT INTO session_meta (session_id, tenant_id, human_takeover)
           VALUES ($1, $2, $3)
           ON CONFLICT (session_id) DO UPDATE SET human_takeover = EXCLUDED.human_takeover""",
        sid, tid, body.enabled,
    )
    await _audit("proactive.takeover", {"contact_id": contact_id, "enabled": body.enabled}, tid=tid)
    return {"ok": True, "session_id": sid, "human_takeover": body.enabled}


class _ProReplyBody(_BaseModel):
    text: str


@app.post("/admin/api/proactive/conversations/{contact_id}/reply", dependencies=[Depends(_check_bot404)])
async def proactive_manual_reply(contact_id: int, body: _ProReplyBody, x_admin_token: str = Header(default="")):
    """Оператор отправляет сообщение из ЛК в user-mode Telegram-диалог.

    Использует tg_client.send_message с phone аккаунта кампании и tg_user_id
    контакта. Пишет в outreach_messages direction='out' — сообщение появится
    во вкладке «Диалоги» (proactive) как ответ оператора.
    """
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    if len(text) > 4000:
        text = text[:4000]

    tid = await _tenant_id_from_token(x_admin_token)
    if tid is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    pool = await _b404_pool()
    contact = await pool.fetchrow(
        """SELECT cc.tg_user_id, cc.username, cc.session_id, c.tenant_id, a.phone
             FROM campaign_contacts cc
             JOIN campaigns    c ON c.id = cc.campaign_id
             JOIN tg_accounts  a ON a.id = c.account_id
            WHERE cc.id = $1""",
        contact_id,
    )
    if not contact:
        raise HTTPException(status_code=404, detail="contact not found")
    if int(contact["tenant_id"]) != int(tid):
        raise HTTPException(status_code=403, detail="not your contact")
    if not contact["tg_user_id"]:
        raise HTTPException(status_code=400, detail="no tg_user_id on contact — reply не поддерживается пока клиент не написал первым")

    # Отправляем через user-mode Telegram
    from proactive import tg_client as _tgc
    try:
        await _tgc.send_message(contact["phone"], int(contact["tg_user_id"]), text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"tg send fail: {e}")

    # Логируем в БД:
    #   - outreach_messages — для UI-диалога «Проактивность → Диалоги»
    #   - bot_404_log — КРИТИЧНО: контекст, который читает бот в /api/sales-chat при
    #     генерации ответа. Без этой записи вернувший управление боту клиент получит
    #     ответ так, как будто оператор не писал ничего — контекст потеряется.
    sid = contact["session_id"] or f"proactive-tg:{contact_id}"
    await pool.execute(
        "INSERT INTO outreach_messages (contact_id, direction, content) VALUES ($1,'out',$2)",
        contact_id, text,
    )
    await pool.execute(
        "INSERT INTO bot_404_log(session_id,direction,text,tenant_id) VALUES($1,'out',$2,$3)",
        sid, text, tid,
    )
    await _audit("proactive.reply", {"contact_id": contact_id, "len": len(text)}, tid=tid)
    return {"ok": True}


# ── Универсальный поиск для Cmd+K палитры ─────────────────────────────────────
@app.get("/admin/api/search", dependencies=[Depends(_check_bot404)])
async def admin_search(q: str = "", limit: int = 12, x_admin_token: str = Header(default="")):
    """Универсальный поиск по диалогам, лидам и сообщениям (bot_404_*)."""
    q = (q or "").strip()
    if not q or len(q) < 2:
        return {"results": []}
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    if not tid:
        return {"results": []}
    like = f"%{q}%"
    results: list[dict] = []

    leads = await pool.fetch(
        """SELECT id, session_id, name, phone, email, telegram, company, created_at
           FROM bot_404_leads
           WHERE tenant_id=$3 AND (name ILIKE $1 OR phone ILIKE $1 OR email ILIKE $1
                 OR telegram ILIKE $1 OR company ILIKE $1)
           ORDER BY created_at DESC LIMIT $2""",
        like, limit, tid,
    )
    for r in leads:
        label = r["name"] or r["phone"] or r["email"] or r["telegram"] or r["company"] or f"lead#{r['id']}"
        contact = " · ".join(x for x in (r["phone"], r["email"], r["telegram"]) if x)
        results.append({
            "type": "lead",
            "id": str(r["id"]),
            "label": label,
            "desc": contact or (r["company"] or "лид"),
            "session_id": r["session_id"],
        })

    msgs = await pool.fetch(
        """SELECT session_id, text, direction, created_at
           FROM bot_404_log
           WHERE tenant_id=$3 AND text ILIKE $1
           ORDER BY id DESC LIMIT $2""",
        like, limit, tid,
    )
    for r in msgs:
        txt = (r["text"] or "")[:120]
        results.append({
            "type": "message",
            "id": r["session_id"],
            "label": "«" + txt + "»",
            "desc": f"сессия {r['session_id'][:18]}… · {r['direction']}",
            "session_id": r["session_id"],
        })

    sids = await pool.fetch(
        "SELECT DISTINCT session_id FROM bot_404_log WHERE tenant_id=$3 AND session_id ILIKE $1 LIMIT $2",
        like, limit, tid,
    )
    for r in sids:
        results.append({
            "type": "session",
            "id": r["session_id"],
            "label": r["session_id"],
            "desc": "session id",
            "session_id": r["session_id"],
        })

    return {"results": results[:limit * 2]}


# ── Sparkline-история за последние 24ч (для метрик дашборда) ──────────────────
@app.get("/admin/api/stats/series", dependencies=[Depends(_check_bot404)])
async def admin_stats_series(range_: str = "24h", x_admin_token: str = Header(default="")):
    """Возвращает массивы значений по часам за последние 24ч (24 точки)."""
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    rows = await pool.fetch(
        """SELECT EXTRACT(EPOCH FROM (now() - created_at))::int / 3600 AS h, count(*)::int AS c
           FROM bot_404_log WHERE tenant_id=$1 AND created_at > now() - interval '24 hours'
           GROUP BY 1 ORDER BY 1""", tid,
    )
    sessions_rows = await pool.fetch(
        """SELECT EXTRACT(EPOCH FROM (now() - min_at))::int / 3600 AS h, count(*)::int AS c
           FROM (SELECT session_id, min(created_at) AS min_at FROM bot_404_log
                 WHERE tenant_id=$1 AND created_at > now() - interval '24 hours' GROUP BY session_id) s
           GROUP BY 1 ORDER BY 1""", tid,
    )
    leads_rows = await pool.fetch(
        """SELECT EXTRACT(EPOCH FROM (now() - created_at))::int / 3600 AS h, count(*)::int AS c
           FROM bot_404_leads WHERE tenant_id=$1 AND created_at > now() - interval '24 hours'
           GROUP BY 1 ORDER BY 1""", tid,
    )

    def fill(rows):
        arr = [0] * 24
        for r in rows:
            h = int(r["h"] or 0)
            if 0 <= h < 24:
                arr[23 - h] = int(r["c"] or 0)
        return arr

    return {
        "range": "24h",
        "messages":  fill(rows),
        "sessions":  fill(sessions_rows),
        "leads":     fill(leads_rows),
        "hot":       [0] * 24,   # placeholder — нет таблицы BANT
        "warm":      [0] * 24,
        "cold":      [0] * 24,
        "escalated": [0] * 24,
    }


# ── ЛК 404: Брендинг виджета (tenant=aisha hardcoded для текущего scope) ──────
# В мульти-тенантном будущем slug определять по login. Сейчас только aisha.

_TENANT_SLUG = "aisha"  # hardcoded scope для acc404_login (в будущем — из login)


# ── Phase 5: audit log helper ────────────────────────────────────────────────
async def _audit(action: str, payload: dict | None, slug: str | None = None, actor_email: str = "tenant", tid: int | None = None):
    try:
        pool = await _b404_pool()
        # Изоляция тенантов: пишем в аудит-лог ИМЕННО того тенанта, кто совершил действие.
        # Раньше при отсутствии slug дефолт был "aisha" → чужие действия (team/branding/tg_bot)
        # писались в аудит Аиши и светились в её ЛК. Теперь: есть tid — берём его; иначе slug; иначе не пишем.
        if tid is None:
            if not slug:
                return
            tid = await pool.fetchval("SELECT id FROM tenants WHERE slug=$1", slug)
        if tid is None:
            return
        import json as _json
        await pool.execute(
            "INSERT INTO tenant_audit_log(actor_email, action, target_tenant_id, payload) VALUES($1,$2,$3,$4)",
            actor_email, action, tid, (_json.dumps(payload) if payload is not None else None),
        )
    except Exception as e:
        print(f"[audit] {e}")


# Audit-лог в ЛК (читает свой)
@app.get("/admin/api/audit", dependencies=[Depends(_check_bot404)])
async def admin_audit(limit: int = 100, x_admin_token: str = Header(default="")):
    if limit < 1 or limit > 500:
        limit = 100
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    if tid is None:
        return {"events": []}
    rows = await pool.fetch(
        """SELECT id, ts, actor_email, action, payload, ip FROM tenant_audit_log
           WHERE target_tenant_id=$1 ORDER BY ts DESC LIMIT $2""",
        tid, limit,
    )
    return {"events": [dict(r) for r in rows]}


@app.get("/admin/api/branding", dependencies=[Depends(_check_bot404)])
async def get_branding(x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    row = await pool.fetchrow("SELECT * FROM v_tenant_branding WHERE tenant_id=$1", tid)
    if not row:
        raise HTTPException(status_code=404, detail="tenant not found")
    return {"branding": dict(row)}


class _BrandingBody(_BaseModel):
    brand_name: str | None = None
    bot_name: str | None = None
    role_subtitle: str | None = None
    logo_url: str | None = None
    primary_color: str | None = None
    accent_color: str | None = None
    text_color: str | None = None
    greeting: str | None = None
    nudge_text: str | None = None
    chat_title: str | None = None
    footer_text: str | None = None
    manager_email: str | None = None
    position: str | None = None


@app.post("/admin/api/branding", dependencies=[Depends(_check_bot404_admin)])
async def set_branding(body: _BrandingBody, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    tid_row = {"id": tid} if tid else None
    if not tid_row:
        raise HTTPException(status_code=404, detail="tenant not found")
    tid = tid_row["id"]
    await pool.execute("INSERT INTO tenant_branding(tenant_id) VALUES($1) ON CONFLICT (tenant_id) DO NOTHING", tid)
    cols = ["brand_name","bot_name","role_subtitle","logo_url","primary_color","accent_color","text_color",
            "greeting","nudge_text","chat_title","footer_text","manager_email","position"]
    data = body.model_dump(exclude_none=False)
    sets = []
    params = []
    i = 1
    for c in cols:
        v = data.get(c, None)
        if v is None and c not in data:
            continue
        sets.append(f"{c}=${i}")
        params.append(v if v != "" else None)
        i += 1
    if not sets:
        return {"ok": True, "noop": True}
    sets.append("updated_at=now()")
    params.append(tid)
    await pool.execute(f"UPDATE tenant_branding SET {', '.join(sets)} WHERE tenant_id=${i}", *params)
    await _audit("branding.update", {k: v for k, v in data.items() if v is not None}, tid=tid)
    return {"ok": True}


# ── Расписание бота в Avito (per-tenant, JSONB в tenant_integrations.avito_schedule) ────
import datetime as _dt_sched
_SCHED_WEEKDAYS = ('mon','tue','wed','thu','fri','sat','sun')

def _sched_valid_time(s):
    if not isinstance(s, str): return False
    parts = s.split(':')
    if len(parts) != 2: return False
    try: h, m = int(parts[0]), int(parts[1])
    except ValueError: return False
    return 0 <= h <= 24 and 0 <= m <= 59

def _sched_valid_spec(spec):
    if not isinstance(spec, dict): return False, "spec must be object"
    if spec.get('always_active') or spec.get('always_off'): return True, None
    wins = spec.get('windows', [])
    if not isinstance(wins, list): return False, "windows must be array"
    for w in wins:
        if not isinstance(w, dict) or not _sched_valid_time(w.get('from')) or not _sched_valid_time(w.get('to')):
            return False, "window must be {from:'HH:MM', to:'HH:MM'}"
        fh, fm = map(int, w['from'].split(':'))
        th, tm = map(int, w['to'].split(':'))
        if fh*60+fm >= th*60+tm:
            return False, f"window '{w['from']}-{w['to']}' invalid: 'from' must be < 'to'"
    return True, None

def _sched_validate(sched):
    if sched is None: return True, None
    if not isinstance(sched, dict): return False, "schedule must be object or null"
    weekly = sched.get('weekly', {})
    if not isinstance(weekly, dict): return False, "weekly must be object"
    for dow, spec in weekly.items():
        if dow not in _SCHED_WEEKDAYS: return False, f"invalid weekday: {dow}"
        ok, err = _sched_valid_spec(spec)
        if not ok: return False, f"weekly.{dow}: {err}"
    overrides = sched.get('overrides', [])
    if not isinstance(overrides, list): return False, "overrides must be array"
    for i, o in enumerate(overrides):
        if not isinstance(o, dict): return False, f"overrides[{i}] must be object"
        try: _dt_sched.date.fromisoformat(o.get('date', ''))
        except ValueError: return False, f"overrides[{i}].date must be YYYY-MM-DD"
        ok, err = _sched_valid_spec(o)
        if not ok: return False, f"overrides[{i}]: {err}"
    return True, None

def _sched_spec_matches(spec, minutes):
    if not spec: return False
    if spec.get('always_active'): return True
    if spec.get('always_off'): return False
    for w in spec.get('windows', []):
        fh, fm = map(int, w['from'].split(':'))
        th, tm = map(int, w['to'].split(':'))
        f, t = fh*60+fm, th*60+tm
        if f < t and f <= minutes < t: return True
    return False

def _sched_evaluate_now(schedule):
    if not schedule: return {'active': True, 'source': 'no_schedule_default_on'}
    now_utc = _dt_sched.datetime.utcnow()
    msk = now_utc + _dt_sched.timedelta(hours=3)
    date_iso = msk.date().isoformat()
    dow = _SCHED_WEEKDAYS[msk.weekday()]
    minutes = msk.hour*60 + msk.minute
    todays_override = next((o for o in schedule.get('overrides', []) if o.get('date') == date_iso), None)
    spec = todays_override or schedule.get('weekly', {}).get(dow)
    return {
        'active': _sched_spec_matches(spec, minutes),
        'source': 'override' if todays_override else 'weekly',
        'msk_now': msk.strftime('%Y-%m-%d %H:%M'),
        'weekday': dow,
    }

@app.get("/admin/api/avito-schedule", dependencies=[Depends(_check_bot404)])
async def get_avito_schedule(x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    if not tid:
        raise HTTPException(status_code=404, detail="tenant not found")
    trow = await pool.fetchrow("SELECT slug, name FROM tenants WHERE id=$1", tid)
    srow = await pool.fetchrow("SELECT avito_schedule FROM tenant_integrations WHERE tenant_id=$1", tid)
    schedule = srow['avito_schedule'] if srow else None
    if isinstance(schedule, str):
        import json as _j
        try: schedule = _j.loads(schedule)
        except Exception: schedule = None
    return {
        'tenant_slug': trow['slug'] if trow else None,
        'tenant_name': trow['name'] if trow else None,
        'schedule': schedule,
        'status': _sched_evaluate_now(schedule),
    }

class _AvitoScheduleBody(_BaseModel):
    schedule: dict | None = None

@app.put("/admin/api/avito-schedule", dependencies=[Depends(_check_bot404_admin)])
async def put_avito_schedule(body: _AvitoScheduleBody, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    if not tid:
        raise HTTPException(status_code=404, detail="tenant not found")
    ok, err = _sched_validate(body.schedule)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    import json as _j
    await pool.execute("INSERT INTO tenant_integrations(tenant_id) VALUES($1) ON CONFLICT (tenant_id) DO NOTHING", tid)
    await pool.execute(
        "UPDATE tenant_integrations SET avito_schedule = $1::jsonb WHERE tenant_id = $2",
        (_j.dumps(body.schedule) if body.schedule is not None else None),
        tid,
    )
    await _audit("avito_schedule.update", {'schedule': body.schedule}, tid=tid)
    return {'ok': True, 'status': _sched_evaluate_now(body.schedule)}


@app.get("/admin/api/branding", dependencies=[Depends(_check_bot404)])
async def get_branding(x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    row = await pool.fetchrow("SELECT * FROM v_tenant_branding WHERE tenant_id=$1", tid)
    if not row:
        raise HTTPException(status_code=404, detail="tenant not found")
    return {"branding": dict(row)}


class _BrandingBody(_BaseModel):
    brand_name: str | None = None
    bot_name: str | None = None
    role_subtitle: str | None = None
    logo_url: str | None = None
    primary_color: str | None = None
    accent_color: str | None = None
    text_color: str | None = None
    greeting: str | None = None
    nudge_text: str | None = None
    chat_title: str | None = None
    footer_text: str | None = None
    manager_email: str | None = None
    position: str | None = None


@app.post("/admin/api/branding", dependencies=[Depends(_check_bot404_admin)])
async def set_branding(body: _BrandingBody, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    tid_row = {"id": tid} if tid else None
    if not tid_row:
        raise HTTPException(status_code=404, detail="tenant not found")
    tid = tid_row["id"]
    await pool.execute("INSERT INTO tenant_branding(tenant_id) VALUES($1) ON CONFLICT (tenant_id) DO NOTHING", tid)
    cols = ["brand_name","bot_name","role_subtitle","logo_url","primary_color","accent_color","text_color",
            "greeting","nudge_text","chat_title","footer_text","manager_email","position"]
    data = body.model_dump(exclude_none=False)
    sets = []
    params = []
    i = 1
    for c in cols:
        v = data.get(c, None)
        if v is None and c not in data:
            continue
        sets.append(f"{c}=${i}")
        params.append(v if v != "" else None)
        i += 1
    if not sets:
        return {"ok": True, "noop": True}
    sets.append("updated_at=now()")
    params.append(tid)
    await pool.execute(f"UPDATE tenant_branding SET {', '.join(sets)} WHERE tenant_id=${i}", *params)
    await _audit("branding.update", {k: v for k, v in data.items() if v is not None}, tid=tid)
    return {"ok": True}


# ═════════════════════════════════════════════════════════════════════════════
# PRM v1.1 API — новая модель integrations → tenants → operators/contacts.
# По ТЗ v1.1 (ответ на 22 замечания PRM Online от 03.07.2026).
# ═════════════════════════════════════════════════════════════════════════════

import bcrypt as _bcrypt_prm
import secrets as _secrets_prm
import datetime as _dt_prm


# ── Rate-limits (ТЗ v1.1 § 4.5) ─────────────────────────────────────────────
# 3-х уровневая защита:
#   primary   = (integration_id, actor_external_id) : 60/мин  — по актору
#   secondary = integration_id                       : 600/мин — по интеграции
#   ip        = client_ip                            : 1000/мин — защитный
import time as _time_prm


async def _prm_check_rate_limit(bucket_key: str, limit_per_min: int) -> tuple[bool, int]:
    """Sliding window через Redis Sorted Set. Возвращает (allowed, current_count)."""
    r = await _get_redis()
    if not r:
        return (True, 0)  # если Redis недоступен — не блокируем (мягкий fallback)
    now_ms = int(_time_prm.time() * 1000)
    window_ms = 60 * 1000
    key = f"prm:rl:{bucket_key}"
    try:
        async with r.pipeline(transaction=False) as pipe:
            pipe.zremrangebyscore(key, 0, now_ms - window_ms)
            pipe.zcard(key)
            pipe.zadd(key, {f"{now_ms}:{_secrets_prm.token_hex(4)}": now_ms})
            pipe.expire(key, 65)
            res = await pipe.execute()
        current = res[1] + 1  # +1 = только что добавленный
        return (current <= limit_per_min, current)
    except Exception:
        return (True, 0)


def _prm_client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return xff or (request.client.host if request.client else "unknown")


# ── аутентификация: Authorization: Bearer <API_CREDENTIAL> ──────────────────
async def _prm_auth(request: Request) -> dict:
    """Возвращает { integration_id, integration_name, allowed_origins, status }.
    Кидает 401 при невалидном credential, 429 при превышении rate-limit.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer credential")
    cred = auth[7:].strip()
    if not cred.startswith("prm_"):
        raise HTTPException(status_code=401, detail="invalid credential format")
    pool = await _b404_pool()
    rows = await pool.fetch(
        "SELECT id, name, api_credential_hash, api_credential_hash_prev, "
        "       api_credential_prev_expires_at, allowed_origins, status "
        "FROM integrations WHERE status='active'"
    )
    matched = None
    for r in rows:
        # проверяем текущий hash
        if _bcrypt_prm.checkpw(cred.encode(), r["api_credential_hash"].encode()):
            matched = r
            break
        # проверяем prev hash (переходный период 24ч после ротации)
        try:
            prev_hash = r.get("api_credential_hash_prev") if hasattr(r, "get") else None
            prev_exp = r.get("api_credential_prev_expires_at") if hasattr(r, "get") else None
        except Exception:
            prev_hash, prev_exp = None, None
        if prev_hash and prev_exp:
            now = _dt_prm.datetime.now(_dt_prm.timezone.utc)
            if prev_exp > now and _bcrypt_prm.checkpw(cred.encode(), prev_hash.encode()):
                matched = r
                break
    if not matched:
        raise HTTPException(status_code=401, detail="invalid credential")

    # secondary: integration_id → 600/мин
    ok, _ = await _prm_check_rate_limit(f"integ:{matched['id']}", 600)
    if not ok:
        raise HTTPException(status_code=429, detail="rate limit exceeded (per integration: 600/min)")
    # primary: (integration_id, endpoint) как proxy для per-actor
    # (актор известен только внутри embed-session endpoints; используем path)
    ep_bucket = f"integ:{matched['id']}:ep:{request.url.path}"
    ok2, _ = await _prm_check_rate_limit(ep_bucket, 60)
    if not ok2:
        raise HTTPException(status_code=429, detail="rate limit exceeded (per endpoint: 60/min)")
    return {
        "integration_id": matched["id"],
        "integration_name": matched["name"],
        "allowed_origins": list(matched["allowed_origins"] or []),
        "status": matched["status"],
    }


async def _prm_audit(integration_id: int, request: Request, path: str,
                     status_code: int, payload_masked: dict | None = None):
    try:
        pool = await _b404_pool()
        import json as _j
        ip = request.client.host if request.client else None
        await pool.execute(
            "INSERT INTO integration_audit (integration_id, actor_ip, method, path, status_code, payload_masked) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            integration_id, ip, request.method, path, status_code,
            (_j.dumps(payload_masked) if payload_masked else None)
        )
    except Exception as e:
        print(f"[prm-audit] {e}")

# ── Task 5: PRM email worker (offline-акторы) ────────────────────────────────
import smtplib as _smtplib_prm
from email.message import EmailMessage as _EmailMessage_prm

async def _prm_send_pending_emails() -> dict:
    """Обходит новые события за последний час, шлёт batched email оффлайн-акторам.
    Возвращает {'emails_sent': N, 'events_covered': M} для health/логов.
    Silent no-op если SMTP не настроен."""
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return {"emails_sent": 0, "events_covered": 0, "reason": "SMTP_HOST not set"}
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "").strip()
    pw   = os.environ.get("SMTP_PASS", "").strip()
    starttls = os.environ.get("SMTP_STARTTLS", "true").lower() != "false"
    default_from = os.environ.get("SMTP_FROM", "no-reply@dirizher404.ru").strip()

    pool = await _b404_pool()
    rows = await pool.fetch("""
        SELECT ie.integration_id, ie.actor_id, ie.actor_type, ie.tenant_id,
               array_agg(ie.id ORDER BY ie.id) AS event_ids,
               array_agg(ie.event_type ORDER BY ie.id) AS event_types,
               array_agg(ie.payload::text ORDER BY ie.id) AS payloads,
               COUNT(*) AS n
        FROM integration_events ie
        WHERE ie.email_sent_at IS NULL
          AND ie.actor_id IS NOT NULL
          AND ie.created_at > now() - interval '1 hour'
        GROUP BY ie.integration_id, ie.actor_id, ie.actor_type, ie.tenant_id
    """)
    if not rows:
        return {"emails_sent": 0, "events_covered": 0}

    emails_sent = 0
    events_covered = 0
    for r in rows:
        online = await pool.fetchval("""
            SELECT COUNT(*) > 0 FROM embed_sessions
            WHERE tenant_id=$1 AND actor_id=$2 AND actor_type=$3
              AND (last_activity_at IS NULL OR last_activity_at > now() - interval '30 minutes')
              AND revoked_at IS NULL AND expires_at > now()
        """, r["tenant_id"], r["actor_id"], r["actor_type"])
        if online:
            await pool.execute(
                "UPDATE integration_events SET email_sent_at=now() WHERE id = ANY($1)",
                r["event_ids"]
            )
            events_covered += r["n"]
            continue

        table = ("tenant_operators" if r["actor_type"] == "operator"
                 else "tenant_contacts" if r["actor_type"] == "contact"
                 else None)
        if not table:
            await pool.execute(
                "UPDATE integration_events SET email_sent_at=now() WHERE id = ANY($1)",
                r["event_ids"]
            )
            events_covered += r["n"]
            continue

        actor = await pool.fetchrow(
            f"SELECT email, name FROM {table} WHERE id=$1 AND active=true",
            r["actor_id"]
        )
        if not actor or not actor["email"]:
            await pool.execute(
                "UPDATE integration_events SET email_sent_at=now() WHERE id = ANY($1)",
                r["event_ids"]
            )
            events_covered += r["n"]
            continue

        integ = await pool.fetchrow(
            "SELECT email_enabled, email_from FROM integrations WHERE id=$1",
            r["integration_id"]
        )
        if integ and integ["email_enabled"] is False:
            await pool.execute(
                "UPDATE integration_events SET email_sent_at=now() WHERE id = ANY($1)",
                r["event_ids"]
            )
            events_covered += r["n"]
            continue

        from_addr = (integ["email_from"] if integ and integ["email_from"] else default_from)

        lines = [f"Здравствуйте, {actor['name'] or ''}!", "",
                 f"В системе появились новые события ({r['n']} шт):"]
        for et, pl in zip(r["event_types"], r["payloads"]):
            lines.append(f"  • {et}: {pl}")
        lines += ["", "Войдите в панель, чтобы посмотреть подробнее.", "", "— Дирижёр"]
        text = "\n".join(lines)
        subject = f"Дирижёр: {r['n']} новых событий в вашем аккаунте"

        try:
            msg = _EmailMessage_prm()
            msg["From"] = from_addr
            msg["To"] = actor["email"]
            msg["Subject"] = subject
            msg.set_content(text)
            if starttls:
                with _smtplib_prm.SMTP(host, port, timeout=15) as s:
                    s.starttls()
                    if user: s.login(user, pw)
                    s.send_message(msg)
            else:
                with _smtplib_prm.SMTP_SSL(host, port, timeout=15) as s:
                    if user: s.login(user, pw)
                    s.send_message(msg)
            emails_sent += 1
            events_covered += r["n"]
            await pool.execute(
                "UPDATE integration_events SET email_sent_at=now() WHERE id = ANY($1)",
                r["event_ids"]
            )
        except Exception as e:
            print(f"[prm-email] send fail to {actor['email']}: {e}")

    return {"emails_sent": emails_sent, "events_covered": events_covered}


@app.post("/prm/api/actors/{actor_id}/notify", tags=["prm-v1.1"])
async def prm_actor_notify_manual(actor_id: int, request: Request):
    """Ручной триггер email-worker (для тестов). Требует _prm_auth."""
    auth = await _prm_auth(request)
    result = await _prm_send_pending_emails()
    await _prm_audit(auth["integration_id"], request, f"/prm/api/actors/{actor_id}/notify", 200, result)
    return result

# ── Task 9: PRM исходящий webhook worker ────────────────────────────────────
import hmac as _hmac_prm
import hashlib as _hashlib_prm
import urllib.request as _urlreq_prm
import urllib.error as _urlerr_prm

async def _prm_deliver_pending_webhooks() -> dict:
    """Обходит недоставленные events, шлёт HMAC-подписанный POST на webhook_url.
    Ретраи с экспоненциальным бэкоффом (5 попыток: 0, 30с, 2м, 8м, 30м — за счёт запусков loop).
    Возвращает {'delivered': N, 'failed': M, 'no_url': K}."""
    pool = await _b404_pool()
    rows = await pool.fetch("""
        SELECT ie.id, ie.integration_id, ie.tenant_id, ie.actor_id, ie.actor_type,
               ie.event_type, ie.payload, ie.created_at, ie.webhook_attempts,
               i.webhook_url, i.webhook_secret
        FROM integration_events ie
        JOIN integrations i ON i.id = ie.integration_id
        WHERE ie.webhook_delivered_at IS NULL
          AND ie.webhook_attempts < 5
          AND i.status = 'active'
        ORDER BY ie.id
        LIMIT 100
    """)
    if not rows:
        return {"delivered": 0, "failed": 0, "no_url": 0}

    delivered = 0
    failed = 0
    no_url = 0
    import json as _j_wh
    for r in rows:
        if not r["webhook_url"]:
            # у integration нет webhook_url — helper polling only, не крутим счётчик
            await pool.execute(
                "UPDATE integration_events SET webhook_delivered_at=now() WHERE id=$1",
                r["id"]
            )
            no_url += 1
            continue

        # экспоненциальный бэкофф: не отправлять чаще чем 30с * 4^attempts
        # (attempts=0 → сразу, 1 → 30с, 2 → 2м, 3 → 8м, 4 → 32м)
        # проверяем через created_at + прошлые попытки — упрощаем: 30с loop сам разгонит

        payload = {
            "event_id": r["id"],
            "event_type": r["event_type"],
            "integration_id": r["integration_id"],
            "tenant_id": r["tenant_id"],
            "actor_id": r["actor_id"],
            "actor_type": r["actor_type"],
            "created_at": r["created_at"].isoformat(),
            "payload": r["payload"],
        }
        body = _j_wh.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

        # HMAC-SHA256 подпись (header X-Dirizher-Signature: sha256=<hex>)
        secret = (r["webhook_secret"] or "").encode("utf-8")
        sig = "sha256=" + _hmac_prm.new(secret, body, _hashlib_prm.sha256).hexdigest() if secret else ""

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Dirizher-Webhook/1.1",
            "X-Dirizher-Event": r["event_type"],
            "X-Dirizher-Event-Id": str(r["id"]),
            "X-Dirizher-Delivery-Attempt": str(r["webhook_attempts"] + 1),
        }
        if sig:
            headers["X-Dirizher-Signature"] = sig

        req = _urlreq_prm.Request(r["webhook_url"], data=body, headers=headers, method="POST")
        try:
            with _urlreq_prm.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    await pool.execute(
                        "UPDATE integration_events SET webhook_delivered_at=now(), webhook_attempts=$1 WHERE id=$2",
                        r["webhook_attempts"] + 1, r["id"]
                    )
                    delivered += 1
                else:
                    raise Exception(f"HTTP {resp.status}")
        except Exception as e:
            err_msg = str(e)[:500]
            await pool.execute(
                "UPDATE integration_events SET webhook_attempts=$1, webhook_last_error=$2 WHERE id=$3",
                r["webhook_attempts"] + 1, err_msg, r["id"]
            )
            failed += 1

    return {"delivered": delivered, "failed": failed, "no_url": no_url}


class _PrmWebhookConfig(_BaseModel):
    webhook_url: str | None = None
    webhook_secret: str | None = None


@app.post("/prm/api/integration/webhook", tags=["prm-v1.1"])
async def prm_set_webhook(body: _PrmWebhookConfig, request: Request):
    """Установить webhook URL + secret для integration.
    NULL webhook_url = отключить исходящий webhook (остаётся polling)."""
    auth = await _prm_auth(request)
    pool = await _b404_pool()
    await pool.execute(
        "UPDATE integrations SET webhook_url=$1, webhook_secret=$2, updated_at=now() WHERE id=$3",
        body.webhook_url, body.webhook_secret, auth["integration_id"]
    )
    await _prm_audit(auth["integration_id"], request, "/prm/api/integration/webhook", 200,
                     {"webhook_url_set": bool(body.webhook_url), "secret_set": bool(body.webhook_secret)})
    return {"ok": True, "webhook_url": body.webhook_url, "secret_configured": bool(body.webhook_secret)}


@app.post("/prm/api/integration/webhook/test", tags=["prm-v1.1"])
async def prm_test_webhook(request: Request):
    """Ручной триггер webhook worker (для дебага). Требует _prm_auth."""
    auth = await _prm_auth(request)
    result = await _prm_deliver_pending_webhooks()
    await _prm_audit(auth["integration_id"], request, "/prm/api/integration/webhook/test", 200, result)
    return result




# ── /prm/api/tenants — компания-клиент, upsert по external_id ───────────────
class _PrmTenantBody(_BaseModel):
    external_id: str
    name: str
    contact_email: str | None = None


@app.post("/prm/api/tenants")
async def prm_create_tenant(body: _PrmTenantBody, request: Request):
    auth = await _prm_auth(request)
    if not body.external_id or not body.name:
        raise HTTPException(status_code=400, detail="external_id and name required")
    pool = await _b404_pool()
    slug = f"prm-{auth['integration_id']}-{body.external_id}".lower()[:60]
    # upsert по (parent_integration_id, external_id)
    row = await pool.fetchrow(
        "SELECT id, slug, name, external_id, status FROM tenants "
        "WHERE parent_integration_id=$1 AND external_id=$2",
        auth["integration_id"], body.external_id
    )
    if row:
        await pool.execute(
            "UPDATE tenants SET name=$1, contact_email=COALESCE($2, contact_email), updated_at=now() "
            "WHERE id=$3", body.name, body.contact_email, row["id"]
        )
        tid = row["id"]
        created = False
    else:
        # plan_id — берём trial по умолчанию (id=1), integration может позже поменять
        default_plan = await pool.fetchval("SELECT id FROM plans WHERE code='trial' LIMIT 1") or 1
        # slug conflict resolution: если такой slug уже есть — добавляем -N суффикс
        base_slug = slug
        for i in range(50):
            slug_try = base_slug if i == 0 else f"{base_slug[:57]}-{i}"
            existing = await pool.fetchval("SELECT id FROM tenants WHERE slug=$1", slug_try)
            if not existing:
                slug = slug_try
                break
        tid = await pool.fetchval(
            "INSERT INTO tenants (slug, name, contact_email, enabled, plan_id, parent_integration_id, external_id, status) "
            "VALUES ($1, $2, $3, true, $4, $5, $6, 'active') RETURNING id",
            slug, body.name, body.contact_email, default_plan, auth["integration_id"], body.external_id
        )
        created = True
    await _prm_audit(auth["integration_id"], request, "/prm/api/tenants", 200,
                     {"external_id": body.external_id, "created": created})
    return {"tenant_id": tid, "external_id": body.external_id, "created": created}


@app.patch("/prm/api/tenants/{tenant_id}")
async def prm_patch_tenant(tenant_id: int, body: dict, request: Request):
    auth = await _prm_auth(request)
    pool = await _b404_pool()
    owner = await pool.fetchval(
        "SELECT id FROM tenants WHERE id=$1 AND parent_integration_id=$2",
        tenant_id, auth["integration_id"]
    )
    if not owner:
        raise HTTPException(status_code=404, detail="tenant not found")
    fields = {k: v for k, v in body.items() if k in ("name", "contact_email")}
    if not fields:
        return {"ok": True, "noop": True}
    sets = ", ".join(f"{k}=${i+1}" for i, k in enumerate(fields.keys()))
    await pool.execute(
        f"UPDATE tenants SET {sets}, updated_at=now() WHERE id=${len(fields)+1}",
        *fields.values(), tenant_id
    )
    await _prm_audit(auth["integration_id"], request, f"/prm/api/tenants/{tenant_id}", 200, fields)
    return {"ok": True, "updated": list(fields.keys())}


@app.post("/prm/api/tenants/{tenant_id}/pause")
async def prm_pause_tenant(tenant_id: int, request: Request):
    auth = await _prm_auth(request)
    pool = await _b404_pool()
    owner = await pool.fetchval(
        "SELECT id FROM tenants WHERE id=$1 AND parent_integration_id=$2",
        tenant_id, auth["integration_id"]
    )
    if not owner:
        raise HTTPException(status_code=404, detail="tenant not found")
    await pool.execute("UPDATE tenants SET status='paused', enabled=false, updated_at=now() WHERE id=$1", tenant_id)
    # revoke все embed-сессии тенанта
    revoked = await pool.execute(
        "UPDATE embed_sessions SET revoked_at=now() WHERE tenant_id=$1 AND revoked_at IS NULL",
        tenant_id
    )
    await _prm_audit(auth["integration_id"], request, f"/prm/api/tenants/{tenant_id}/pause", 200,
                     {"tenant_id": tenant_id})
    return {"ok": True, "tenant_id": tenant_id, "status": "paused"}


@app.post("/prm/api/tenants/{tenant_id}/resume")
async def prm_resume_tenant(tenant_id: int, request: Request):
    auth = await _prm_auth(request)
    pool = await _b404_pool()
    owner = await pool.fetchval(
        "SELECT id FROM tenants WHERE id=$1 AND parent_integration_id=$2",
        tenant_id, auth["integration_id"]
    )
    if not owner:
        raise HTTPException(status_code=404, detail="tenant not found")
    await pool.execute("UPDATE tenants SET status='active', enabled=true, updated_at=now() WHERE id=$1", tenant_id)
    await _prm_audit(auth["integration_id"], request, f"/prm/api/tenants/{tenant_id}/resume", 200,
                     {"tenant_id": tenant_id})
    return {"ok": True, "tenant_id": tenant_id, "status": "active"}


# ── /prm/api/tenants/{tid}/operators — админ компании, upsert ────────────────
class _PrmActorBody(_BaseModel):
    external_id: str
    email: str | None = None
    name: str | None = None
    role: str | None = "admin"
    perms: dict | None = None
    # Task 4 (v1.2): parent_contact_id для sub-partners
    parent_external_id: str | None = None


@app.post("/prm/api/tenants/{tenant_id}/operators")
async def prm_create_operator(tenant_id: int, body: _PrmActorBody, request: Request):
    auth = await _prm_auth(request)
    if not body.external_id:
        raise HTTPException(status_code=400, detail="external_id required")
    pool = await _b404_pool()
    owner = await pool.fetchval(
        "SELECT id FROM tenants WHERE id=$1 AND parent_integration_id=$2",
        tenant_id, auth["integration_id"]
    )
    if not owner:
        raise HTTPException(status_code=404, detail="tenant not found")
    import json as _j
    row = await pool.fetchrow(
        "SELECT id FROM tenant_operators WHERE tenant_id=$1 AND external_id=$2",
        tenant_id, body.external_id
    )
    perms_json = _j.dumps(body.perms) if body.perms is not None else None
    if row:
        await pool.execute(
            "UPDATE tenant_operators SET email=COALESCE($1,email), name=COALESCE($2,name), "
            "role=COALESCE($3,role), perms=COALESCE($4::jsonb,perms), active=true WHERE id=$5",
            body.email, body.name, body.role, perms_json, row["id"]
        )
        oid = row["id"]
        created = False
    else:
        oid = await pool.fetchval(
            "INSERT INTO tenant_operators (tenant_id, external_id, email, name, role, perms) "
            "VALUES ($1, $2, $3, $4, $5, COALESCE($6::jsonb, '{}'::jsonb)) RETURNING id",
            tenant_id, body.external_id, body.email, body.name, body.role or "admin", perms_json
        )
        created = True
    await _prm_audit(auth["integration_id"], request, f"/prm/api/tenants/{tenant_id}/operators", 200,
                     {"external_id": body.external_id, "created": created})
    return {"operator_id": oid, "external_id": body.external_id, "created": created}


@app.post("/prm/api/tenants/{tenant_id}/contacts")
async def prm_create_contact(tenant_id: int, body: _PrmActorBody, request: Request):
    auth = await _prm_auth(request)
    if not body.external_id:
        raise HTTPException(status_code=400, detail="external_id required")
    pool = await _b404_pool()
    owner = await pool.fetchval(
        "SELECT id FROM tenants WHERE id=$1 AND parent_integration_id=$2",
        tenant_id, auth["integration_id"]
    )
    if not owner:
        raise HTTPException(status_code=404, detail="tenant not found")
    row = await pool.fetchrow(
        "SELECT id FROM tenant_contacts WHERE tenant_id=$1 AND external_id=$2",
        tenant_id, body.external_id
    )
    if row:
        # Task 4: обновляем parent_contact_id если задан parent_external_id
        parent_id = None
        if body.parent_external_id:
            parent_id = await pool.fetchval(
                "SELECT id FROM tenant_contacts WHERE tenant_id=$1 AND external_id=$2",
                tenant_id, body.parent_external_id
            )
            if parent_id is None:
                raise HTTPException(status_code=400,
                    detail=f"parent contact {body.parent_external_id} not found in tenant {tenant_id}")
            if parent_id == row["id"]:
                raise HTTPException(status_code=400, detail="contact cannot be its own parent")
        await pool.execute(
            "UPDATE tenant_contacts SET email=COALESCE($1,email), name=COALESCE($2,name), active=true, "
            "parent_contact_id=COALESCE($3, parent_contact_id) WHERE id=$4",
            body.email, body.name, parent_id, row["id"]
        )
        cid = row["id"]
        created = False
    else:
        # Task 4: резолв parent_contact_id по внешнему id
        parent_id = None
        if body.parent_external_id:
            parent_id = await pool.fetchval(
                "SELECT id FROM tenant_contacts WHERE tenant_id=$1 AND external_id=$2",
                tenant_id, body.parent_external_id
            )
            if parent_id is None:
                raise HTTPException(status_code=400,
                    detail=f"parent contact {body.parent_external_id} not found in tenant {tenant_id}")
        cid = await pool.fetchval(
            "INSERT INTO tenant_contacts (tenant_id, external_id, email, name, parent_contact_id) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            tenant_id, body.external_id, body.email, body.name, parent_id
        )
        created = True
    await _prm_audit(auth["integration_id"], request, f"/prm/api/tenants/{tenant_id}/contacts", 200,
                     {"external_id": body.external_id, "created": created})
    return {"contact_id": cid, "external_id": body.external_id, "created": created}


# ── /prm/api/embed-session — opaque one-time code ────────────────────────────
class _PrmEmbedSessionBody(_BaseModel):
    tenant_id: int
    actor_external_id: str
    actor_type: str  # 'operator' | 'contact'
    ttl: int | None = 60


@app.post("/prm/api/embed-session")
async def prm_embed_session(body: _PrmEmbedSessionBody, request: Request):
    auth = await _prm_auth(request)
    if body.actor_type not in ("operator", "contact", "super_admin"):
        raise HTTPException(status_code=400, detail="actor_type must be 'operator', 'contact' or 'super_admin'")
    ttl = max(30, min(120, body.ttl or 60))
    pool = await _b404_pool()
    # проверяем tenant
    t = await pool.fetchrow(
        "SELECT id, status FROM tenants WHERE id=$1 AND parent_integration_id=$2",
        body.tenant_id, auth["integration_id"]
    )
    if not t:
        raise HTTPException(status_code=404, detail="tenant not found")
    if t["status"] != "active":
        raise HTTPException(status_code=403, detail=f"tenant status={t['status']}")
    # ищем актера (super_admin — global scope integration, без tenant-actor)
    if body.actor_type == "super_admin":
        # super_admin: actor_id используется как integration_id (маркер scope);
        # perms сохраняются в embed_sessions.perms = {scope: integration, integration_id}
        actor_id = auth["integration_id"]
        _sa_perms = {"scope": "integration", "integration_id": auth["integration_id"]}
    else:
        table = "tenant_operators" if body.actor_type == "operator" else "tenant_contacts"
        actor = await pool.fetchrow(
            f"SELECT id, active FROM {table} WHERE tenant_id=$1 AND external_id=$2",
            body.tenant_id, body.actor_external_id
        )
        if not actor:
            raise HTTPException(status_code=404, detail=f"{body.actor_type} not found")
        if not actor["active"]:
            raise HTTPException(status_code=403, detail=f"{body.actor_type} inactive")
        actor_id = actor["id"]
        _sa_perms = None
    # генерируем opaque-код и сохраняем
    code = _secrets_prm.token_hex(32)
    now = _dt_prm.datetime.now(_dt_prm.timezone.utc)
    expires = now + _dt_prm.timedelta(seconds=ttl)
    import json as _j_prm
    await pool.execute(
        "INSERT INTO embed_sessions (code, tenant_id, actor_id, actor_type, perms, expires_at) "
        "VALUES ($1, $2, $3, $4, $5::jsonb, $6)",
        code, body.tenant_id, actor_id, body.actor_type,
        (_j_prm.dumps(_sa_perms) if _sa_perms else None), expires
    )
    await _prm_audit(auth["integration_id"], request, "/prm/api/embed-session", 200,
                     {"tenant_id": body.tenant_id, "actor_type": body.actor_type,
                      "actor_external_id": body.actor_external_id})
    # embed_url — на нашем домене (в prod будет CNAME клиента)
    host = request.headers.get("host", "217-149-25-34.sslip.io")
    return {
        "embed_url": f"https://{host}/embed",
        "code": code,
        "expires_in": ttl,
        "expires_at": expires.isoformat(),
    }


# ── /embed — установка сессии + редирект ─────────────────────────────────────
from fastapi import Form as _Form
from fastapi.responses import RedirectResponse as _RedirectResponse, Response as _Response


@app.post("/embed")
async def prm_embed(code: str = _Form(...), mode: str = _Form("cookie"), request: Request = None):
    """
    Установить embed-сессию по одноразовому коду.
    mode:
      'cookie'  (default) — set-cookie + 302 → /admin?embed=1
      'bearer'            — JSON { session_token, redirect_url } — для fallback когда
                            cookies заблокированы (Safari private, uBlock и т.п.)
    """
    # IP-level защитный лимит 1000/мин (для DDoS)
    ip = _prm_client_ip(request)
    ok, _ = await _prm_check_rate_limit(f"embed:ip:{ip}", 1000)
    if not ok:
        raise HTTPException(status_code=429, detail="too many embed attempts from this IP")
    pool = await _b404_pool()
    # атомарный check-and-set: используем UPDATE ... RETURNING
    session_id = _secrets_prm.token_hex(24)
    max_lifetime = _dt_prm.datetime.now(_dt_prm.timezone.utc) + _dt_prm.timedelta(hours=8)
    row = await pool.fetchrow(
        "UPDATE embed_sessions SET used_at=now(), session_id=$1, "
        "session_expires_at=$2, last_activity_at=now() "
        "WHERE code=$3 AND used_at IS NULL AND expires_at > now() AND revoked_at IS NULL "
        "RETURNING tenant_id, actor_id, actor_type",
        session_id, max_lifetime, code
    )
    if not row:
        raise HTTPException(status_code=400, detail="invalid or expired code")
    integ = await pool.fetchrow(
        "SELECT i.id, i.allowed_origins FROM integrations i "
        "JOIN tenants t ON t.parent_integration_id=i.id WHERE t.id=$1", row["tenant_id"]
    )
    origins = " ".join(list(integ["allowed_origins"] or [])) if integ else ""

    # Bearer-режим: возвращаем JSON вместо cookie+redirect
    if mode == "bearer":
        from fastapi.responses import JSONResponse as _JSONResponse
        resp = _JSONResponse({
            "session_token": session_id,
            "redirect_url": "/admin?embed=1",
            "expires_at": max_lifetime.isoformat(),
            "usage": "Send in header 'X-Session-Token: <token>' or 'Authorization: Bearer sess_<token>' on every /admin/api/embed/* request",
        })
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    # Cookie-режим (default)
    resp = _RedirectResponse(url="/admin?embed=1", status_code=302)
    resp.set_cookie(
        key="orchestra_sess", value=session_id,
        max_age=1800, httponly=True, secure=True, samesite="none", path="/"
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    if origins:
        resp.headers["Content-Security-Policy"] = f"frame-ancestors {origins};"
    return resp


# ── /prm/api/actors/{aid}/revoke — мгновенный logout ─────────────────────────
@app.post("/prm/api/actors/{actor_id}/revoke")
async def prm_revoke(actor_id: int, request: Request):
    auth = await _prm_auth(request)
    actor_type = request.query_params.get("actor_type", "operator")
    if actor_type not in ("operator", "contact", "super_admin"):
        raise HTTPException(status_code=400, detail="actor_type must be 'operator', 'contact' or 'super_admin'")
    pool = await _b404_pool()
    # revoke все активные embed_sessions этого актора в тенантах интеграции
    result = await pool.execute(
        "UPDATE embed_sessions es SET revoked_at=now() "
        "FROM tenants t WHERE es.tenant_id=t.id AND t.parent_integration_id=$1 "
        "AND es.actor_id=$2 AND es.actor_type=$3 AND es.revoked_at IS NULL",
        auth["integration_id"], actor_id, actor_type
    )
    await _prm_audit(auth["integration_id"], request, f"/prm/api/actors/{actor_id}/revoke", 200,
                     {"actor_id": actor_id, "actor_type": actor_type})
    return {"ok": True, "revoked": result}


# ── /prm/api/events — курсорный polling (ТЗ v1.1 § 10) ──────────────────────
_PRM_EVENTS_RETENTION_DAYS = 30


@app.get("/prm/api/events")
async def prm_events(request: Request, since: int = 0, limit: int = 100):
    auth = await _prm_auth(request)
    limit = max(1, min(500, limit))
    pool = await _b404_pool()

    # cursor_reset: если клиент отстал больше чем на retention — сбрасываем на current head
    cursor_reset = False
    if since > 0:
        oldest_id = await pool.fetchval(
            "SELECT MIN(id) FROM integration_events WHERE integration_id=$1",
            auth["integration_id"]
        )
        if oldest_id is not None and since < oldest_id:
            cursor_reset = True
            head = await pool.fetchval(
                "SELECT COALESCE(MAX(id), 0) FROM integration_events WHERE integration_id=$1",
                auth["integration_id"]
            )
            return {
                "events": [],
                "next_cursor": head or 0,
                "cursor_reset": True,
                "reason": "cursor_expired",
                "hint": f"events older than {_PRM_EVENTS_RETENTION_DAYS} days have been retained-out; resuming from head",
            }

    rows = await pool.fetch(
        "SELECT id, tenant_id, actor_id, actor_type, event_type, payload, created_at "
        "FROM integration_events WHERE integration_id=$1 AND id > $2 "
        "ORDER BY id ASC LIMIT $3",
        auth["integration_id"], since, limit
    )
    events = [{
        "id": r["id"], "tenant_id": r["tenant_id"], "actor_id": r["actor_id"],
        "actor_type": r["actor_type"], "event_type": r["event_type"],
        "payload": r["payload"], "created_at": r["created_at"].isoformat()
    } for r in rows]
    next_cursor = events[-1]["id"] if events else since
    return {"events": events, "next_cursor": next_cursor}


# ── /prm/api/whoami — для отладки авторизации ────────────────────────────────
@app.get("/prm/api/whoami")
async def prm_whoami(request: Request):
    auth = await _prm_auth(request)
    return {"integration_id": auth["integration_id"], "name": auth["integration_name"],
            "allowed_origins": auth["allowed_origins"], "status": auth["status"]}


# ── /health/prm — публичный healthcheck (без auth, для мониторинга) ──────────
@app.get("/health/prm")
async def prm_health():
    """Проверка живости PRM v1.1 API: БД, Redis, ключевые таблицы.
    Возвращает 200 если всё ok, 503 при сбое любой компоненты."""
    checks = {"db": False, "redis": False, "tables": False, "worker": False}
    errors = []
    # 1. DB
    try:
        pool = await _b404_pool()
        r = await pool.fetchval("SELECT 1")
        checks["db"] = r == 1
    except Exception as e:
        errors.append(f"db: {e}")
    # 2. Redis
    try:
        r = await _get_redis()
        if r:
            await r.ping()
            checks["redis"] = True
        else:
            errors.append("redis: not initialised")
    except Exception as e:
        errors.append(f"redis: {e}")
    # 3. Ключевые таблицы
    try:
        pool = await _b404_pool()
        tables = ("integrations", "tenant_operators", "tenant_contacts",
                  "embed_sessions", "integration_events", "integration_audit")
        for t in tables:
            _ = await pool.fetchval(f"SELECT COUNT(*) FROM {t}")
        checks["tables"] = True
    except Exception as e:
        errors.append(f"tables: {e}")
    # 4. Worker retention (проверяем что events не переполнены — не старше 30д есть)
    try:
        pool = await _b404_pool()
        oldest = await pool.fetchval(
            "SELECT MIN(created_at) FROM integration_events"
        )
        if oldest is None:
            checks["worker"] = True  # пустая — норма
        else:
            import datetime as _dt
            age = (_dt.datetime.now(_dt.timezone.utc) - oldest).days
            checks["worker"] = age <= 32  # запас 2 дня
            if not checks["worker"]:
                errors.append(f"worker: oldest event {age} days > 32")
    except Exception as e:
        errors.append(f"worker: {e}")

    status_code = 200 if all(checks.values()) else 503
    from fastapi.responses import JSONResponse as _JSONResp
    return _JSONResp(
        {"ok": all(checks.values()), "checks": checks, "errors": errors},
        status_code=status_code
    )


# ── /prm/api/integration/rotate-credential — ротация API credential ──────────
# ТЗ v1.1 § 4: ротация с переходным периодом 24 часа (оба credential валидны).
# Требует старый credential в Authorization. Возвращает НОВЫЙ credential один раз.
@app.post("/prm/api/integration/rotate-credential")
async def prm_rotate_credential(request: Request):
    auth = await _prm_auth(request)
    pool = await _b404_pool()
    # генерируем новый
    new_cred = "prm_" + _secrets_prm.token_hex(32)
    new_hash = _bcrypt_prm.hashpw(new_cred.encode(), _bcrypt_prm.gensalt(rounds=12)).decode()
    # для переходного периода 24ч храним ОБА hash'а в одной строке integrations через доп. поле.
    # Простая схема: колонка api_credential_hash_prev + api_credential_prev_expires_at.
    # Проверим что колонки есть; если нет — миграция на лету.
    try:
        await pool.execute(
            "ALTER TABLE integrations ADD COLUMN IF NOT EXISTS api_credential_hash_prev text; "
            "ALTER TABLE integrations ADD COLUMN IF NOT EXISTS api_credential_prev_expires_at timestamptz;"
        )
    except Exception:
        pass
    # сохраняем ТЕКУЩИЙ hash как prev, новый — в основную колонку
    grace_expires = _dt_prm.datetime.now(_dt_prm.timezone.utc) + _dt_prm.timedelta(hours=24)
    await pool.execute(
        "UPDATE integrations SET "
        "  api_credential_hash_prev = api_credential_hash, "
        "  api_credential_prev_expires_at = $1, "
        "  api_credential_hash = $2, "
        "  updated_at = now() "
        "WHERE id = $3",
        grace_expires, new_hash, auth["integration_id"]
    )
    await _prm_audit(auth["integration_id"], request, "/prm/api/integration/rotate-credential", 200,
                     {"grace_period_hours": 24, "prev_expires_at": grace_expires.isoformat()})
    return {
        "new_credential": new_cred,       # ПОКАЗАН ОДИН РАЗ
        "prev_valid_until": grace_expires.isoformat(),
        "grace_period_hours": 24,
        "warning": "Save this credential IMMEDIATELY. Only bcrypt-hash is stored server-side. "
                   "Previous credential is still valid for 24h (transition period).",
    }


# ── DELETE /prm/api/tenants/{id} — soft-delete + retention 30 дней ───────────
@app.delete("/prm/api/tenants/{tenant_id}")
async def prm_delete_tenant(tenant_id: int, request: Request):
    auth = await _prm_auth(request)
    pool = await _b404_pool()
    owner = await pool.fetchrow(
        "SELECT id, status FROM tenants WHERE id=$1 AND parent_integration_id=$2",
        tenant_id, auth["integration_id"]
    )
    if not owner:
        raise HTTPException(status_code=404, detail="tenant not found")
    scheduled = _dt_prm.datetime.now(_dt_prm.timezone.utc) + _dt_prm.timedelta(days=30)
    await pool.execute(
        "UPDATE tenants SET status='pending_deletion', enabled=false, "
        "delete_scheduled_at=$1, updated_at=now() WHERE id=$2",
        scheduled, tenant_id
    )
    # немедленно revoked все embed-сессии тенанта
    await pool.execute(
        "UPDATE embed_sessions SET revoked_at=now() WHERE tenant_id=$1 AND revoked_at IS NULL",
        tenant_id
    )
    await _prm_audit(auth["integration_id"], request, f"/prm/api/tenants/{tenant_id}", 200,
                     {"soft_delete": True, "scheduled_at": scheduled.isoformat()})
    return {
        "ok": True, "tenant_id": tenant_id, "status": "pending_deletion",
        "delete_scheduled_at": scheduled.isoformat(),
        "restore_before": scheduled.isoformat(),
    }


@app.post("/prm/api/tenants/{tenant_id}/restore")
async def prm_restore_tenant(tenant_id: int, request: Request):
    auth = await _prm_auth(request)
    pool = await _b404_pool()
    row = await pool.fetchrow(
        "SELECT id, status, delete_scheduled_at FROM tenants "
        "WHERE id=$1 AND parent_integration_id=$2",
        tenant_id, auth["integration_id"]
    )
    if not row:
        raise HTTPException(status_code=404, detail="tenant not found")
    if row["status"] != "pending_deletion":
        raise HTTPException(status_code=400, detail=f"tenant status={row['status']}, only pending_deletion can be restored")
    await pool.execute(
        "UPDATE tenants SET status='active', enabled=true, delete_scheduled_at=NULL, updated_at=now() "
        "WHERE id=$1", tenant_id
    )
    await _prm_audit(auth["integration_id"], request, f"/prm/api/tenants/{tenant_id}/restore", 200,
                     {"tenant_id": tenant_id})
    return {"ok": True, "tenant_id": tenant_id, "status": "active"}


# ── DELETE operator / contact — пометить inactive ───────────────────────────
@app.delete("/prm/api/tenants/{tenant_id}/operators/{operator_id}")
async def prm_delete_operator(tenant_id: int, operator_id: int, request: Request):
    auth = await _prm_auth(request)
    pool = await _b404_pool()
    owner = await pool.fetchval(
        "SELECT id FROM tenants WHERE id=$1 AND parent_integration_id=$2",
        tenant_id, auth["integration_id"]
    )
    if not owner:
        raise HTTPException(status_code=404, detail="tenant not found")
    result = await pool.execute(
        "UPDATE tenant_operators SET active=false WHERE id=$1 AND tenant_id=$2 AND active=true",
        operator_id, tenant_id
    )
    if result.endswith("0"):
        raise HTTPException(status_code=404, detail="operator not found or already inactive")
    # revoked все embed_sessions этого оператора
    await pool.execute(
        "UPDATE embed_sessions SET revoked_at=now() "
        "WHERE tenant_id=$1 AND actor_id=$2 AND actor_type='operator' AND revoked_at IS NULL",
        tenant_id, operator_id
    )
    await _prm_audit(auth["integration_id"], request, f"/prm/api/tenants/{tenant_id}/operators/{operator_id}", 200,
                     {"deactivated": True})
    return {"ok": True, "operator_id": operator_id, "active": False}


@app.delete("/prm/api/tenants/{tenant_id}/contacts/{contact_id}")
async def prm_delete_contact(tenant_id: int, contact_id: int, request: Request):
    auth = await _prm_auth(request)
    pool = await _b404_pool()
    owner = await pool.fetchval(
        "SELECT id FROM tenants WHERE id=$1 AND parent_integration_id=$2",
        tenant_id, auth["integration_id"]
    )
    if not owner:
        raise HTTPException(status_code=404, detail="tenant not found")
    result = await pool.execute(
        "UPDATE tenant_contacts SET active=false WHERE id=$1 AND tenant_id=$2 AND active=true",
        contact_id, tenant_id
    )
    if result.endswith("0"):
        raise HTTPException(status_code=404, detail="contact not found or already inactive")
    await pool.execute(
        "UPDATE embed_sessions SET revoked_at=now() "
        "WHERE tenant_id=$1 AND actor_id=$2 AND actor_type='contact' AND revoked_at IS NULL",
        tenant_id, contact_id
    )
    await _prm_audit(auth["integration_id"], request, f"/prm/api/tenants/{tenant_id}/contacts/{contact_id}", 200,
                     {"deactivated": True})
    return {"ok": True, "contact_id": contact_id, "active": False}


# ── /prm/api/tenants/{id}/export — async job ────────────────────────────────
@app.post("/prm/api/tenants/{tenant_id}/export")
async def prm_export_tenant(tenant_id: int, request: Request):
    auth = await _prm_auth(request)
    pool = await _b404_pool()
    owner = await pool.fetchval(
        "SELECT id FROM tenants WHERE id=$1 AND parent_integration_id=$2",
        tenant_id, auth["integration_id"]
    )
    if not owner:
        raise HTTPException(status_code=404, detail="tenant not found")
    job_id = "job_" + _secrets_prm.token_hex(12)
    import json as _j
    await pool.execute(
        "INSERT INTO async_jobs (id, integration_id, tenant_id, job_type, status, result) "
        "VALUES ($1, $2, $3, 'export', 'queued', $4::jsonb)",
        job_id, auth["integration_id"], tenant_id, _j.dumps({"requested_at": _dt_prm.datetime.now(_dt_prm.timezone.utc).isoformat()})
    )
    # реальный worker вычитывает из async_jobs где status='queued' и обрабатывает.
    # для MVP инкапсулируем в фоновой корутине асинхронно (для реального prod — Celery/RQ).
    import asyncio as _aio
    _aio.create_task(_prm_run_export_job(job_id, tenant_id))
    await _prm_audit(auth["integration_id"], request, f"/prm/api/tenants/{tenant_id}/export", 200,
                     {"job_id": job_id})
    return {"job_id": job_id, "status": "queued"}


async def _prm_run_export_job(job_id: str, tenant_id: int):
    """MVP-реализация экспорта: собирает основные данные тенанта в JSON и сохраняет в async_jobs.result.
    В prod вместо этого — стриминг в S3 и signed URL."""
    try:
        pool = await _b404_pool()
        await pool.execute("UPDATE async_jobs SET status='processing', updated_at=now() WHERE id=$1", job_id)
        # собираем данные
        tenant = await pool.fetchrow("SELECT id, slug, name, external_id, status, created_at FROM tenants WHERE id=$1", tenant_id)
        operators = await pool.fetch("SELECT id, external_id, email, name, role, active FROM tenant_operators WHERE tenant_id=$1", tenant_id)
        contacts = await pool.fetch("SELECT id, external_id, email, name, active FROM tenant_contacts WHERE tenant_id=$1", tenant_id)
        sessions = await pool.fetch("SELECT COUNT(*) AS total FROM embed_sessions WHERE tenant_id=$1", tenant_id)
        import json as _j
        export = {
            "tenant": {**dict(tenant), "created_at": tenant["created_at"].isoformat()},
            "operators": [dict(o) for o in operators],
            "contacts": [dict(c) for c in contacts],
            "sessions_total": sessions[0]["total"],
            "generated_at": _dt_prm.datetime.now(_dt_prm.timezone.utc).isoformat(),
        }
        # готовый архив кладём прямо в result.data — для MVP; в prod → S3
        await pool.execute(
            "UPDATE async_jobs SET status='done', result=$1::jsonb, updated_at=now() WHERE id=$2",
            _j.dumps({"data": export, "download_url": None, "note": "MVP: data inline; prod будет S3 signed URL"}),
            job_id
        )
    except Exception as e:
        import json as _j
        pool = await _b404_pool()
        await pool.execute("UPDATE async_jobs SET status='failed', result=$1::jsonb, updated_at=now() WHERE id=$2",
                           _j.dumps({"error": str(e)}), job_id)


@app.get("/prm/api/jobs/{job_id}")
async def prm_get_job(job_id: str, request: Request):
    auth = await _prm_auth(request)
    pool = await _b404_pool()
    row = await pool.fetchrow(
        "SELECT id, job_type, status, result, created_at, updated_at "
        "FROM async_jobs WHERE id=$1 AND integration_id=$2",
        job_id, auth["integration_id"]
    )
    if not row:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "job_id": row["id"], "job_type": row["job_type"], "status": row["status"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "result": row["result"],
    }


# ── Cookie-based embed-session (Этап E.1) ────────────────────────────────────
# Читает cookie orchestra_sess, находит активную сессию, проверяет idle/max timeout,
# продлевает last_activity_at. Не пересекается со старым JWT-auth: если cookie нет —
# возвращает None и вызывающий код работает по старой цепочке.
_PRM_IDLE_TIMEOUT_MIN = 30
_PRM_MAX_LIFETIME_HR  = 8


async def _get_embed_session(request: Request) -> dict | None:
    # ТЗ v1.1 § 4.4: bearer-fallback — если cookies заблокированы (Safari private / uBlock),
    # клиент передаёт токен в заголовке. Приоритет — cookie, потом header.
    cookie_session = request.cookies.get("orchestra_sess")
    session_id = cookie_session
    if not session_id:
        auth_hdr = request.headers.get("Authorization", "")
        # маркер 'Bearer sess_' — отличаем от 'Bearer prm_' (это API credential, не сессия)
        if auth_hdr.startswith("Bearer sess_"):
            session_id = auth_hdr[len("Bearer sess_"):].strip()
    if not session_id:
        session_id = request.headers.get("X-Session-Token", "").strip() or None
    if not session_id:
        return None
    pool = await _b404_pool()
    now = _dt_prm.datetime.now(_dt_prm.timezone.utc)
    idle_cutoff = now - _dt_prm.timedelta(minutes=_PRM_IDLE_TIMEOUT_MIN)
    row = await pool.fetchrow(
        "SELECT es.tenant_id, es.actor_id, es.actor_type, es.perms, "
        "       es.session_expires_at, es.last_activity_at, es.revoked_at, "
        "       t.parent_integration_id, t.status AS tenant_status "
        "FROM embed_sessions es JOIN tenants t ON t.id = es.tenant_id "
        "WHERE es.session_id = $1",
        session_id
    )
    if not row:
        return None
    if row["revoked_at"] is not None:
        return None
    if row["session_expires_at"] and row["session_expires_at"] < now:
        return None  # max-timeout истёк
    if row["last_activity_at"] and row["last_activity_at"] < idle_cutoff:
        return None  # idle-timeout истёк
    if row["tenant_status"] != "active":
        return None
    # проверяем что актор ещё активен (super_admin — без active-check, actor_id = integration_id)
    if row["actor_type"] == "super_admin":
        pass  # super_admin валиден пока сессия не истекла/не отозвана
    else:
        table = "tenant_operators" if row["actor_type"] == "operator" else "tenant_contacts"
        active = await pool.fetchval(
            f"SELECT active FROM {table} WHERE id=$1", row["actor_id"]
        )
        if not active:
            return None
    # CSRF-защита (только для cookie-режима: bearer не автоматически отправляется браузером).
    # ТЗ v1.1 § 4 безопасность.
    if cookie_session:
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin:
            from urllib.parse import urlparse
            try:
                p = urlparse(origin)
                origin_norm = f"{p.scheme}://{p.netloc}"
            except Exception:
                origin_norm = origin
            allowed_origins = await pool.fetchval(
                "SELECT allowed_origins FROM integrations WHERE id=$1",
                row["parent_integration_id"]
            ) or []
            # same-origin (наш собственный домен) — разрешаем (для тестов + custom-domain).
            # За reverse-proxy (Caddy) request.url отдаёт internal URL — используем публичный Host.
            fwd_proto = request.headers.get("x-forwarded-proto", "https")
            fwd_host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
            public_origin = f"{fwd_proto}://{fwd_host}"
            same_origin_ok = origin_norm == public_origin
            if not (same_origin_ok or origin_norm in allowed_origins):
                raise HTTPException(status_code=403, detail=f"CSRF: origin '{origin_norm}' not in allowed_origins")
        elif request.method not in ("GET", "HEAD"):
            # POST/PUT/DELETE без Origin/Referer — потенциальный CSRF, блокируем
            raise HTTPException(status_code=403, detail="CSRF: missing Origin/Referer on write request")
    # продлеваем last_activity_at
    await pool.execute(
        "UPDATE embed_sessions SET last_activity_at=now() WHERE session_id=$1",
        session_id
    )
    return {
        "tenant_id": row["tenant_id"],
        "actor_id": row["actor_id"],
        "actor_type": row["actor_type"],
        "perms": row["perms"] or {},
        "integration_id": row["parent_integration_id"],
        "session_id": session_id,
        "session_expires_at": row["session_expires_at"],
    }


@app.get("/admin/api/embed/whoami")
async def embed_whoami(request: Request):
    """Проверка активной embed-сессии. Возвращает 200 c данными актора или 401."""
    sess = await _get_embed_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail="no active embed session")
    return {
        "tenant_id": sess["tenant_id"],
        "actor_id": sess["actor_id"],
        "actor_type": sess["actor_type"],
        "integration_id": sess["integration_id"],
        "perms": sess["perms"],
        "session_expires_at": sess["session_expires_at"].isoformat() if sess["session_expires_at"] else None,
    }


@app.post("/admin/api/embed/logout")
async def embed_logout(request: Request):
    """Явный logout текущей embed-сессии.
    CSRF-защита через _require_embed (проверка Origin для cookie-режима)."""
    sess = await _require_embed(request)  # 403 если чужой Origin, 401 если нет сессии
    pool = await _b404_pool()
    result = await pool.execute(
        "UPDATE embed_sessions SET revoked_at=now() "
        "WHERE session_id=$1 AND revoked_at IS NULL",
        sess["session_id"]
    )
    return {"ok": True, "was_active": not result.endswith("0")}


# ── Embed-endpoints с RBAC (Этап E.2) ────────────────────────────────────────
# Отдельные endpoints для iframe-режима PRM Online.
# Все старые /admin/api/* (Заря, Аиша) — без изменений, работают через JWT.


async def _require_embed(request: Request) -> dict:
    # CSRF-проверка теперь внутри _get_embed_session (единая точка).
    sess = await _get_embed_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail="no active embed session")
    return sess


@app.get("/admin/api/embed/leads")
async def embed_leads(request: Request, status: str | None = None, search: str | None = None):
    """Лиды в embed-режиме. Operator видит все лиды тенанта; contact — только те,
    у которых assigned_contact_id = actor.id."""
    sess = await _require_embed(request)
    pool = await _b404_pool()
    where = ["l.tenant_id = $1"]
    params: list = [sess["tenant_id"]]
    if sess["actor_type"] == "contact":
        params.append(sess["actor_id"])
        where.append(f"l.assigned_contact_id = ${len(params)}")
    if status:
        params.append(status)
        where.append(f"l.status = ${len(params)}")
    if search:
        params.append(f"%{search}%")
        idx = len(params)
        where.append(
            f"(l.name ILIKE ${idx} OR l.phone ILIKE ${idx} OR l.email ILIKE ${idx} "
            f"OR l.telegram ILIKE ${idx} OR l.company ILIKE ${idx})"
        )
    rows = await pool.fetch(
        f"""SELECT l.id, l.session_id, l.name, l.phone, l.email, l.telegram, l.company, l.note,
                   l.created_at, l.updated_at, l.status,
                   l.assigned_operator_id, l.assigned_contact_id
              FROM bot_404_leads l
             WHERE {" AND ".join(where)}
             ORDER BY l.updated_at DESC NULLS LAST, l.created_at DESC
             LIMIT 500""",
        *params,
    )
    return {
        "leads": [dict(r) for r in rows],
        "actor_type": sess["actor_type"],
        "actor_id": sess["actor_id"],
        "tenant_id": sess["tenant_id"],
    }


@app.get("/admin/api/embed/sessions")
async def embed_sessions_list(request: Request):
    """Список чат-сессий (диалогов) в embed-режиме.
    Operator видит все чаты тенанта; contact видит только чаты, где session_id связан с
    лидом с assigned_contact_id = actor.id."""
    sess = await _require_embed(request)
    pool = await _b404_pool()
    if sess["actor_type"] == "contact":
        # чаты, у которых есть лид, назначенный этому контакту
        rows = await pool.fetch(
            """SELECT DISTINCT l.session_id, l.name, l.phone, l.status, l.updated_at
                 FROM bot_404_leads l
                WHERE l.tenant_id = $1 AND l.assigned_contact_id = $2
                ORDER BY l.updated_at DESC NULLS LAST LIMIT 200""",
            sess["tenant_id"], sess["actor_id"],
        )
    else:
        # operator видит все чаты тенанта
        rows = await pool.fetch(
            """SELECT DISTINCT session_id, MAX(created_at) AS last_ts
                 FROM bot_404_log
                WHERE tenant_id = $1
                GROUP BY session_id
                ORDER BY last_ts DESC LIMIT 200""",
            sess["tenant_id"],
        )
    return {
        "sessions": [dict(r) for r in rows],
        "actor_type": sess["actor_type"],
    }


@app.get("/admin/api/embed/sessions/{session_id}/messages")
async def embed_session_messages(session_id: str, request: Request):
    """Сообщения одного диалога. Contact получает 403 если session не связан с его лидом."""
    sess = await _require_embed(request)
    pool = await _b404_pool()
    if sess["actor_type"] == "contact":
        allowed = await pool.fetchval(
            "SELECT 1 FROM bot_404_leads WHERE session_id=$1 AND tenant_id=$2 AND assigned_contact_id=$3 LIMIT 1",
            session_id, sess["tenant_id"], sess["actor_id"],
        )
        if not allowed:
            raise HTTPException(status_code=403, detail="not your conversation")
    rows = await pool.fetch(
        "SELECT direction, text, created_at FROM bot_404_log "
        "WHERE session_id=$1 AND tenant_id=$2 ORDER BY id ASC LIMIT 500",
        session_id, sess["tenant_id"],
    )
    return {"messages": [dict(r) for r in rows]}


@app.get("/admin/api/embed/stats")
async def embed_stats(request: Request):
    """Статистика тенанта. Contact видит только счётчик своих открытых лидов; operator — сводную."""
    sess = await _require_embed(request)
    pool = await _b404_pool()
    if sess["actor_type"] == "contact":
        row = await pool.fetchrow(
            """SELECT COUNT(*) FILTER (WHERE status IN ('new','in_progress')) AS my_active,
                      COUNT(*) FILTER (WHERE status='client') AS my_deals
                 FROM bot_404_leads
                WHERE tenant_id=$1 AND assigned_contact_id=$2""",
            sess["tenant_id"], sess["actor_id"],
        )
        return {"scope": "contact", "my_active": row[0], "my_deals": row[1]}
    else:
        row = await pool.fetchrow(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE status='new') AS new_count,
                      COUNT(*) FILTER (WHERE status='client') AS deals
                 FROM bot_404_leads WHERE tenant_id=$1""",
            sess["tenant_id"],
        )
        return {
            "scope": "operator", "total": row[0],
            "new_count": row[1], "deals": row[2],
        }


@app.get("/admin/api/embed/settings")
async def embed_settings_get(request: Request):
    """Настройки виджета — доступ только оператору. Contact получает 403."""
    sess = await _require_embed(request)
    if sess["actor_type"] != "operator":
        raise HTTPException(status_code=403, detail="settings available for operators only")
    pool = await _b404_pool()
    row = await pool.fetchrow(
        "SELECT * FROM v_tenant_branding WHERE tenant_id=$1", sess["tenant_id"]
    )
    return {"branding": dict(row) if row else None}


# ── Task 14: super_admin — список всех тенантов integration ────────────────
@app.get("/admin/api/embed/tenants", tags=["prm-v1.1"])
async def embed_tenants_list(request: Request):
    """Список всех тенантов integration (только для super_admin).
    Operator/contact получают 403."""
    sess = await _require_embed(request)
    if sess["actor_type"] != "super_admin":
        raise HTTPException(status_code=403, detail="tenants list available for super_admin only")
    pool = await _b404_pool()
    rows = await pool.fetch(
        "SELECT t.id, t.slug, t.name, t.external_id, t.status, t.created_at, "
        "       (SELECT COUNT(*) FROM bot_404_leads WHERE tenant_id=t.id) AS leads_count "
        "FROM tenants t WHERE t.parent_integration_id = $1 "
        "ORDER BY t.id",
        sess["integration_id"]
    )
    return {
        "tenants": [dict(r) for r in rows],
        "integration_id": sess["integration_id"],
        "count": len(rows),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Task 10a: полный ЛК в iframe — управление ботом через embed-endpoints
# RBAC: read — все акторы, write — operator + super_admin (contact → 403)
# ═══════════════════════════════════════════════════════════════════════════

def _embed_can_write(sess: dict) -> bool:
    """Может ли актор писать (изменять). Contact — нет."""
    return sess["actor_type"] in ("operator", "super_admin")


# ── Промпт бота ─────────────────────────────────────────────────────────────
@app.get("/admin/api/embed/prompt", tags=["prm-v1.1"])
async def embed_prompt_get(request: Request):
    """Читать system_prompt тенанта. Read — доступно всем акторам с валидной сессией."""
    sess = await _require_embed(request)
    pool = await _b404_pool()
    row = await pool.fetchrow(
        "SELECT system_prompt FROM tenant_integrations WHERE tenant_id=$1",
        sess["tenant_id"]
    )
    return {
        "tenant_id": sess["tenant_id"],
        "system_prompt": (row["system_prompt"] if row else "") or "",
        "editable": _embed_can_write(sess),
    }


class _EmbedPromptBody(_BaseModel):
    system_prompt: str


@app.patch("/admin/api/embed/prompt", tags=["prm-v1.1"])
async def embed_prompt_patch(body: _EmbedPromptBody, request: Request):
    """Обновить system_prompt тенанта. Только operator/super_admin."""
    sess = await _require_embed(request)
    if not _embed_can_write(sess):
        raise HTTPException(status_code=403, detail="prompt edit not allowed for this actor_type")
    p = (body.system_prompt or "").strip()
    if len(p) < 20:
        raise HTTPException(status_code=400, detail="system_prompt too short (min 20 chars)")
    if len(p) > 100000:
        raise HTTPException(status_code=400, detail="system_prompt too long (max 100000 chars)")
    pool = await _b404_pool()
    # upsert в tenant_integrations
    exists = await pool.fetchval(
        "SELECT 1 FROM tenant_integrations WHERE tenant_id=$1", sess["tenant_id"]
    )
    if exists:
        await pool.execute(
            "UPDATE tenant_integrations SET system_prompt=$1 WHERE tenant_id=$2",
            p, sess["tenant_id"]
        )
    else:
        await pool.execute(
            "INSERT INTO tenant_integrations (tenant_id, system_prompt) VALUES ($1, $2)",
            sess["tenant_id"], p
        )
    return {"ok": True, "length": len(p)}


# ── Knowledge Base (SQL напрямую — VectorStore.list_docs не поддерживает tenant filter) ──
@app.get("/admin/api/embed/knowledge", tags=["prm-v1.1"])
async def embed_knowledge_list(request: Request, category: str | None = None, search: str | None = None, limit: int = 200):
    """Список документов KB тенанта. Фильтры category/search."""
    sess = await _require_embed(request)
    pool = await _b404_pool()
    conds = ["tenant_id = $1"]
    params = [sess["tenant_id"]]
    i = 2
    if category:
        conds.append(f"category = ${i}"); params.append(category); i += 1
    if search:
        conds.append(f"(title ILIKE ${i} OR content ILIKE ${i})")
        params.append(f"%{search}%"); i += 1
    params.append(min(500, max(1, limit)))
    rows = await pool.fetch(
        f"SELECT id, title, category, LEFT(content, 400) AS preview, "
        f"       LENGTH(content) AS length, "
        f"       to_char(updated_at, 'YYYY-MM-DD HH24:MI') AS updated_at "
        f"FROM knowledge_base "
        f"WHERE {' AND '.join(conds)} "
        f"ORDER BY category NULLS LAST, title NULLS LAST "
        f"LIMIT ${i}",
        *params
    )
    return {
        "documents": [dict(r) for r in rows],
        "count": len(rows),
        "editable": _embed_can_write(sess),
    }


class _EmbedKnowledgeItem(_BaseModel):
    id: str | None = None
    title: str | None = None
    category: str | None = None
    content: str
    source: str | None = None


class _EmbedKnowledgeBulk(_BaseModel):
    items: list[_EmbedKnowledgeItem]


@app.post("/admin/api/embed/knowledge", tags=["prm-v1.1"])
async def embed_knowledge_upsert(body: _EmbedKnowledgeBulk, request: Request):
    """Добавить/обновить документы в KB одним батчем.
    Формат: {items: [{id?, title?, category?, content, source?}, ...]}
    ID автоматически префиксуется tenant_id (защита от коллизий между тенантами).
    Только operator/super_admin."""
    sess = await _require_embed(request)
    if not _embed_can_write(sess):
        raise HTTPException(status_code=403, detail="knowledge write not allowed")
    if not body.items or len(body.items) > 100:
        raise HTTPException(status_code=400, detail="items: 1..100 required")
    tid = sess["tenant_id"]
    # OpenAI embeddings
    import os as _os
    from openai import OpenAI as _OpenAI
    client = _OpenAI(api_key=_os.environ.get("OPENAI_API_KEY"), base_url=_os.environ.get("OPENAI_BASE_URL") or None)
    from knowledge.vector_store import _get_conn as _kb_conn
    conn = _kb_conn(); conn.autocommit = True
    inserted = 0
    for idx, item in enumerate(body.items):
        text = (item.content or "").strip()
        if not text:
            continue
        if len(text) > 50000:
            raise HTTPException(status_code=400, detail=f"item[{idx}].content too long (max 50000)")
        raw_id = item.id or f"embed-{tid}-{_secrets_prm.token_hex(4)}"
        doc_id = raw_id if raw_id.startswith(f"{tid}::") else f"{tid}::{raw_id}"
        try:
            emb_resp = client.embeddings.create(
                model=_os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
                input=text
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"embedding failed: {e}")
        emb = "[" + ",".join(str(x) for x in emb_resp.data[0].embedding) + "]"
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO knowledge_base (id, content, embedding, category, title, source, tenant_id)
                   VALUES (%s, %s, %s::vector, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET
                     content=EXCLUDED.content, embedding=EXCLUDED.embedding,
                     category=EXCLUDED.category, title=EXCLUDED.title, source=EXCLUDED.source,
                     tenant_id=EXCLUDED.tenant_id, updated_at=now()
                   WHERE knowledge_base.tenant_id = EXCLUDED.tenant_id""",
                (doc_id, text, emb, item.category, item.title, item.source, tid),
            )
        inserted += 1
    return {"ok": True, "inserted": inserted}


@app.delete("/admin/api/embed/knowledge/{doc_id:path}", tags=["prm-v1.1"])
async def embed_knowledge_delete(doc_id: str, request: Request):
    """Удалить документ из KB. Только operator/super_admin.
    Doc_id — полный (`tenant::rawid`) или короткий (`rawid`) — авто-нормализация."""
    sess = await _require_embed(request)
    if not _embed_can_write(sess):
        raise HTTPException(status_code=403, detail="knowledge delete not allowed")
    tid = sess["tenant_id"]
    norm_id = doc_id if doc_id.startswith(f"{tid}::") else f"{tid}::{doc_id}"
    pool = await _b404_pool()
    n = await pool.execute(
        "DELETE FROM knowledge_base WHERE id=$1 AND tenant_id=$2",
        norm_id, tid
    )
    # asyncpg возвращает 'DELETE 1' или 'DELETE 0'
    deleted = 0
    try: deleted = int(n.split()[-1]) if n else 0
    except: pass
    if deleted == 0:
        raise HTTPException(status_code=404, detail="document not found in your tenant")
    return {"ok": True, "deleted": doc_id}


# ── Настройки виджета (PATCH) ──────────────────────────────────────────────
class _EmbedBrandingBody(_BaseModel):
    brand_name: str | None = None
    bot_name: str | None = None
    role_subtitle: str | None = None
    logo_url: str | None = None
    primary_color: str | None = None
    accent_color: str | None = None
    text_color: str | None = None
    greeting: str | None = None
    nudge_text: str | None = None
    chat_title: str | None = None
    footer_text: str | None = None
    manager_email: str | None = None
    position: str | None = None


@app.patch("/admin/api/embed/settings", tags=["prm-v1.1"])
async def embed_settings_patch(body: _EmbedBrandingBody, request: Request):
    """Обновить брендинг виджета. Только operator/super_admin."""
    sess = await _require_embed(request)
    if not _embed_can_write(sess):
        raise HTTPException(status_code=403, detail="settings edit not allowed")
    pool = await _b404_pool()
    tid = sess["tenant_id"]
    await pool.execute(
        "INSERT INTO tenant_branding(tenant_id) VALUES($1) ON CONFLICT (tenant_id) DO NOTHING", tid
    )
    cols = ["brand_name","bot_name","role_subtitle","logo_url","primary_color","accent_color","text_color",
            "greeting","nudge_text","chat_title","footer_text","manager_email","position"]
    data = body.model_dump(exclude_none=False)
    sets, params = [], []
    i = 1
    for c in cols:
        v = data.get(c, None)
        if v is not None:
            sets.append(f"{c}=${i}")
            params.append(v); i += 1
    if not sets:
        return {"ok": True, "updated": 0}
    params.append(tid)
    await pool.execute(f"UPDATE tenant_branding SET {', '.join(sets)} WHERE tenant_id=${i}", *params)
    return {"ok": True, "updated": len(sets)}


# ── Расписание бота (Avito + виджет) ────────────────────────────────────────
@app.get("/admin/api/embed/schedule", tags=["prm-v1.1"])
async def embed_schedule_get(request: Request):
    """Получить avito_schedule тенанта + вычисленный статус (активен/неактивен сейчас)."""
    sess = await _require_embed(request)
    pool = await _b404_pool()
    row = await pool.fetchrow(
        "SELECT avito_schedule FROM tenant_integrations WHERE tenant_id=$1",
        sess["tenant_id"]
    )
    sched = row["avito_schedule"] if row else None
    if isinstance(sched, str):
        import json as _j
        try: sched = _j.loads(sched)
        except: sched = None
    return {
        "schedule": sched,
        "current": _sched_evaluate_now(sched),
        "editable": _embed_can_write(sess),
    }


class _EmbedScheduleBody(_BaseModel):
    schedule: dict | None = None  # None = отключить расписание (always active)


@app.patch("/admin/api/embed/schedule", tags=["prm-v1.1"])
async def embed_schedule_patch(body: _EmbedScheduleBody, request: Request):
    """Обновить расписание. Только operator/super_admin."""
    sess = await _require_embed(request)
    if not _embed_can_write(sess):
        raise HTTPException(status_code=403, detail="schedule edit not allowed")
    ok, err = _sched_validate(body.schedule)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    pool = await _b404_pool()
    exists = await pool.fetchval(
        "SELECT 1 FROM tenant_integrations WHERE tenant_id=$1", sess["tenant_id"]
    )
    import json as _j
    sched_json = _j.dumps(body.schedule) if body.schedule else None
    if exists:
        await pool.execute(
            "UPDATE tenant_integrations SET avito_schedule=$1::jsonb WHERE tenant_id=$2",
            sched_json, sess["tenant_id"]
        )
    else:
        await pool.execute(
            "INSERT INTO tenant_integrations (tenant_id, avito_schedule) VALUES ($1, $2::jsonb)",
            sess["tenant_id"], sched_json
        )
    return {"ok": True, "schedule_set": body.schedule is not None}


# ── Human Takeover: перехват / ответ / возврат боту ─────────────────────────
@app.post("/admin/api/embed/sessions/{session_id}/takeover", tags=["prm-v1.1"])
async def embed_session_takeover(session_id: str, request: Request):
    """Перехватить диалог живым оператором (бот перестаёт отвечать в этой сессии).
    Только operator/super_admin."""
    sess = await _require_embed(request)
    if not _embed_can_write(sess):
        raise HTTPException(status_code=403, detail="takeover not allowed for this actor_type")
    pool = await _b404_pool()
    # проверяем что сессия принадлежит нашему тенанту
    exists = await pool.fetchval(
        "SELECT 1 FROM bot_404_log WHERE session_id=$1 AND tenant_id=$2 LIMIT 1",
        session_id, sess["tenant_id"]
    )
    if not exists:
        raise HTTPException(status_code=404, detail="session not found in your tenant")
    # маркер takeover в отдельной таблице (создаём if not exists)
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS bot_404_takeover ("
        "  session_id text PRIMARY KEY,"
        "  tenant_id int NOT NULL,"
        "  operator_actor_id int,"
        "  operator_actor_type text,"
        "  taken_at timestamptz DEFAULT now(),"
        "  released_at timestamptz"
        ")"
    )
    await pool.execute(
        "INSERT INTO bot_404_takeover (session_id, tenant_id, operator_actor_id, operator_actor_type) "
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (session_id) DO UPDATE SET released_at=NULL, taken_at=now(), "
        "operator_actor_id=EXCLUDED.operator_actor_id, operator_actor_type=EXCLUDED.operator_actor_type",
        session_id, sess["tenant_id"], sess["actor_id"], sess["actor_type"]
    )
    return {"ok": True, "session_id": session_id, "taken_by_actor_id": sess["actor_id"]}


class _EmbedTakeoverReply(_BaseModel):
    text: str


@app.post("/admin/api/embed/sessions/{session_id}/reply", tags=["prm-v1.1"])
async def embed_session_reply(session_id: str, body: _EmbedTakeoverReply, request: Request):
    """Оператор пишет в чат от имени бота. Только после takeover."""
    sess = await _require_embed(request)
    if not _embed_can_write(sess):
        raise HTTPException(status_code=403, detail="reply not allowed")
    txt = (body.text or "").strip()
    if not txt:
        raise HTTPException(status_code=400, detail="text required")
    if len(txt) > 4000:
        raise HTTPException(status_code=400, detail="text too long (max 4000)")
    pool = await _b404_pool()
    # проверим что диалог перехвачен нами
    t = await pool.fetchrow(
        "SELECT operator_actor_id, released_at FROM bot_404_takeover "
        "WHERE session_id=$1 AND tenant_id=$2",
        session_id, sess["tenant_id"]
    )
    if not t or t["released_at"] is not None:
        raise HTTPException(status_code=400, detail="session not under takeover — call /takeover first")
    # пишем ответ в bot_404_log как direction=out
    await pool.execute(
        "INSERT INTO bot_404_log (session_id, direction, text, tenant_id) VALUES ($1, 'out', $2, $3)",
        session_id, txt, sess["tenant_id"]
    )
    return {"ok": True}


@app.post("/admin/api/embed/sessions/{session_id}/release", tags=["prm-v1.1"])
async def embed_session_release(session_id: str, request: Request):
    """Вернуть диалог боту. Только operator/super_admin."""
    sess = await _require_embed(request)
    if not _embed_can_write(sess):
        raise HTTPException(status_code=403, detail="release not allowed")
    pool = await _b404_pool()
    await pool.execute(
        "UPDATE bot_404_takeover SET released_at=now() "
        "WHERE session_id=$1 AND tenant_id=$2 AND released_at IS NULL",
        session_id, sess["tenant_id"]
    )
    return {"ok": True, "session_id": session_id}


@app.get("/admin/api/branding", dependencies=[Depends(_check_bot404)])
async def get_branding(x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    row = await pool.fetchrow("SELECT * FROM v_tenant_branding WHERE tenant_id=$1", tid)
    if not row:
        raise HTTPException(status_code=404, detail="tenant not found")
    return {"branding": dict(row)}


class _BrandingBody(_BaseModel):
    brand_name: str | None = None
    bot_name: str | None = None
    role_subtitle: str | None = None
    logo_url: str | None = None
    primary_color: str | None = None
    accent_color: str | None = None
    text_color: str | None = None
    greeting: str | None = None
    nudge_text: str | None = None
    chat_title: str | None = None
    footer_text: str | None = None
    manager_email: str | None = None
    position: str | None = None


@app.post("/admin/api/branding", dependencies=[Depends(_check_bot404_admin)])
async def set_branding(body: _BrandingBody, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    tid_row = {"id": tid} if tid else None
    if not tid_row:
        raise HTTPException(status_code=404, detail="tenant not found")
    tid = tid_row["id"]
    await pool.execute("INSERT INTO tenant_branding(tenant_id) VALUES($1) ON CONFLICT (tenant_id) DO NOTHING", tid)
    cols = ["brand_name","bot_name","role_subtitle","logo_url","primary_color","accent_color","text_color",
            "greeting","nudge_text","chat_title","footer_text","manager_email","position"]
    data = body.model_dump(exclude_none=False)
    sets = []
    params = []
    i = 1
    for c in cols:
        v = data.get(c, None)
        if v is None and c not in data:
            continue
        sets.append(f"{c}=${i}")
        params.append(v if v != "" else None)
        i += 1
    if not sets:
        return {"ok": True, "noop": True}
    sets.append("updated_at=now()")
    params.append(tid)
    await pool.execute(f"UPDATE tenant_branding SET {', '.join(sets)} WHERE tenant_id=${i}", *params)
    await _audit("branding.update", {k: v for k, v in data.items() if v is not None}, tid=tid)
    return {"ok": True}



# ── ЛК 404: Telegram-боты (CRUD) ─────────────────────────────────────────────
import re as _re, os as _os, base64 as _b64, asyncio as _asyncio
from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM

# Phase 9: pub/sub в Redis — bot404 подхватывает изменения
_redis = None


async def _get_redis():
    global _redis
    if _redis is None:
        try:
            import redis.asyncio as redisaio
            _redis = redisaio.from_url(_os.environ.get("REDIS_URL", "redis://chatbot_redis:6379"), decode_responses=True)
        except Exception as e:
            print(f"[redis] init fail: {e}")
            _redis = False
    return _redis if _redis is not False else None


async def _publish_change(channel: str):
    r = await _get_redis()
    if r:
        try:
            await r.publish(channel, "1")
        except Exception as e:
            print(f"[redis] publish fail: {e}")


def _enc_key():
    k = _os.environ.get("BOT_TOKEN_ENC_KEY", "")
    if not k or len(k) != 64:
        return None
    return bytes.fromhex(k)


def _encrypt_secret(plain: str) -> str | None:
    k = _enc_key()
    if not k or not plain:
        return None
    iv = _os.urandom(12)
    ct = _AESGCM(k).encrypt(iv, plain.encode("utf-8"), None)
    # _AESGCM returns ciphertext+tag, we keep tag separate to match Node format (iv|tag|ct)
    # cryptography puts tag at end of ciphertext: ct[:-16] | ct[-16:]
    cipher = ct[:-16]
    tag = ct[-16:]
    return _b64.b64encode(iv + tag + cipher).decode("ascii")


@app.get("/admin/api/tg-bots", dependencies=[Depends(_check_bot404)])
async def list_tg_bots(x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    rows = await pool.fetch(
        """SELECT b.id, b.bot_username, b.bot_id, b.enabled, b.last_seen_at, b.created_at,
                  b.notes, (b.bot_token IS NOT NULL) AS has_token
           FROM tenant_tg_bots b
           WHERE b.tenant_id = $1
           ORDER BY b.id DESC""",
        tid,
    )
    return {"bots": [dict(r) for r in rows]}


class _TgBotBody(_BaseModel):
    bot_token: str


@app.post("/admin/api/tg-bots", dependencies=[Depends(_check_bot404_admin)])
async def add_tg_bot(body: _TgBotBody, x_admin_token: str = Header(default="")):
    token = body.bot_token.strip()
    if not _re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
        raise HTTPException(status_code=400, detail="Неверный формат токена (ожидается 12345678:ABC…)")
    # Валидация через getMe (через тот же Cloudflare Worker что использует bot404)
    import urllib.request, urllib.error, json as _json
    try:
        with urllib.request.urlopen(
            f"https://jolly-union-66fa.gbefhberh.workers.dev/bot{token}/getMe",
            timeout=10,
        ) as r:
            j = _json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не могу достучаться до Telegram: {e}")
    if not j.get("ok"):
        raise HTTPException(status_code=400, detail=f"Telegram: {j.get('description','неверный токен')}")
    me = j["result"]
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    tid_row = {"id": tid} if tid else None
    if not tid_row:
        raise HTTPException(status_code=404, detail="tenant not found")
    # Проверка дубля по bot_id (это надёжнее чем по токену т.к. бот может перевыпустить токен)
    existing = await pool.fetchrow("SELECT id FROM tenant_tg_bots WHERE bot_id=$1", me.get("id"))
    if existing:
        raise HTTPException(status_code=409, detail="Такой бот уже подключён")
    enc_token = _encrypt_secret(token)
    if enc_token:
        bot_id = await pool.fetchval(
            """INSERT INTO tenant_tg_bots(tenant_id, bot_token_enc, bot_id, bot_username, enabled)
               VALUES($1, $2, $3, $4, true) RETURNING id""",
            tid_row["id"], enc_token, me.get("id"), ("@" + me["username"]) if me.get("username") else None,
        )
    else:
        # Fallback: BOT_TOKEN_ENC_KEY не задан — пишем plaintext (warning)
        print("[tg-bot.add] WARN: encrypting disabled, storing plaintext")
        bot_id = await pool.fetchval(
            """INSERT INTO tenant_tg_bots(tenant_id, bot_token, bot_id, bot_username, enabled)
               VALUES($1, $2, $3, $4, true) RETURNING id""",
            tid_row["id"], token, me.get("id"), ("@" + me["username"]) if me.get("username") else None,
        )
    await _audit("tg_bot.add", {"id": bot_id, "username": me.get("username")}, tid=tid)
    await _publish_change("tg-bots-changed")
    return {"ok": True, "id": bot_id, "bot_username": ("@" + me["username"]) if me.get("username") else None,
            "hot_reload": True}


@app.post("/admin/api/tg-bots/{bot_id}/toggle", dependencies=[Depends(_check_bot404)])
async def toggle_tg_bot(bot_id: int, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    new_state = await pool.fetchval(
        """UPDATE tenant_tg_bots SET enabled = NOT enabled
           WHERE id=$1 AND tenant_id=$2 RETURNING enabled""",
        bot_id, tid,
    )
    if new_state is None:
        raise HTTPException(status_code=404, detail="bot not found")
    await _audit("tg_bot.toggle", {"id": bot_id, "enabled": new_state}, tid=tid)
    await _publish_change("tg-bots-changed")
    return {"ok": True, "enabled": new_state, "hot_reload": True}


@app.delete("/admin/api/tg-bots/{bot_id}", dependencies=[Depends(_check_bot404)])
async def delete_tg_bot(bot_id: int, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    deleted = await pool.fetchval(
        """DELETE FROM tenant_tg_bots WHERE id=$1 AND tenant_id=$2 RETURNING id""",
        bot_id, tid,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="bot not found")
    await _audit("tg_bot.delete", {"id": bot_id}, tid=tid)
    await _publish_change("tg-bots-changed")
    return {"ok": True, "hot_reload": True}


# ── ЛК 404: Расход (биллинг) ──────────────────────────────────────────────────

@app.get("/admin/api/usage/summary", dependencies=[Depends(_check_bot404)])
async def usage_summary(x_admin_token: str = Header(default="")):
    """Сводка: лимиты + потребление за сегодня/месяц + остаток баланса провайдера."""
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    lim = await pool.fetchrow(
        "SELECT tenant_id, slug, name, plan_code, model, rpm_limit, daily_tokens_limit, monthly_budget_rub "
        "FROM v_tenant_effective_limits WHERE tenant_id=$1", tid,
    )
    if not lim:
        raise HTTPException(status_code=404, detail="tenant not found")
    today = await pool.fetchrow(
        "SELECT COALESCE(SUM(in_tokens+out_tokens),0) AS tok, COALESCE(SUM(cost_rub_x100),0) AS cost "
        "FROM usage_events WHERE tenant_id=$1 AND ts >= date_trunc('day', now() AT TIME ZONE 'UTC')",
        lim["tenant_id"],
    )
    month = await pool.fetchrow(
        "SELECT COALESCE(SUM(cost_rub_x100),0) AS cost, COUNT(*) AS calls "
        "FROM usage_events WHERE tenant_id=$1 AND ts >= date_trunc('month', now() AT TIME ZONE 'UTC')",
        lim["tenant_id"],
    )
    return {
        "limits": {
            "rpm": lim["rpm_limit"],
            "daily_tokens": lim["daily_tokens_limit"],
            "monthly_budget_rub": lim["monthly_budget_rub"],
            "model": lim["model"],
            "plan": lim["plan_code"],
        },
        "today": {
            "tokens": int(today["tok"] or 0),
            "cost_rub": (int(today["cost"] or 0)) / 100,
        },
        "month": {
            "cost_rub": (int(month["cost"] or 0)) / 100,
            "calls": int(month["calls"] or 0),
        },
    }


@app.get("/admin/api/usage/series", dependencies=[Depends(_check_bot404)])
async def usage_series(days: int = 7, x_admin_token: str = Header(default="")):
    """Серия за N дней (по дням): токены и стоимость."""
    if days < 1 or days > 90:
        days = 7
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    if not tid:
        raise HTTPException(status_code=404, detail="tenant not found")
    rows = await pool.fetch(
        """SELECT date_trunc('day', ts AT TIME ZONE 'UTC')::date AS d,
                  SUM(in_tokens + out_tokens)::int AS tokens,
                  SUM(cost_rub_x100)::int AS cost_x100,
                  COUNT(*)::int AS calls
           FROM usage_events
           WHERE tenant_id=$1 AND ts >= now() - ($2 || ' days')::interval
           GROUP BY 1 ORDER BY 1""",
        tid, str(days),
    )
    # заполняем нулями отсутствующие дни
    from datetime import datetime, timedelta, timezone
    today_utc = datetime.now(timezone.utc).date()
    by_date = {r["d"]: r for r in rows}
    series = []
    for i in range(days - 1, -1, -1):
        d = today_utc - timedelta(days=i)
        r = by_date.get(d)
        series.append({
            "date": d.isoformat(),
            "tokens": int(r["tokens"]) if r else 0,
            "cost_rub": (int(r["cost_x100"]) / 100) if r else 0,
            "calls": int(r["calls"]) if r else 0,
        })
    return {"days": days, "series": series}


@app.get("/admin/api/usage/top-models", dependencies=[Depends(_check_bot404)])
async def usage_top_models(days: int = 30, x_admin_token: str = Header(default="")):
    if days < 1 or days > 365:
        days = 30
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    if not tid:
        raise HTTPException(status_code=404, detail="tenant not found")
    rows = await pool.fetch(
        """SELECT model,
                  COUNT(*)::int AS calls,
                  SUM(in_tokens)::int AS in_tok,
                  SUM(out_tokens)::int AS out_tok,
                  SUM(cost_rub_x100)::int AS cost_x100,
                  AVG(latency_ms)::int AS avg_latency
           FROM usage_events
           WHERE tenant_id=$1 AND ts >= now() - ($2 || ' days')::interval AND model IS NOT NULL
           GROUP BY model ORDER BY cost_x100 DESC""",
        tid, str(days),
    )
    return {"days": days, "models": [
        {"model": r["model"], "calls": r["calls"],
         "in_tokens": r["in_tok"], "out_tokens": r["out_tok"],
         "cost_rub": (r["cost_x100"] or 0) / 100,
         "avg_latency_ms": int(r["avg_latency"] or 0)} for r in rows
    ]}


# ── Phase 11: CSV экспорт ────────────────────────────────────────────────────
import io as _io, csv as _csv
from fastapi.responses import StreamingResponse


def _csv_response(rows: list[dict], filename: str, fieldnames: list[str]):
    buf = _io.StringIO()
    buf.write("﻿")  # BOM для Excel чтобы кириллица читалась
    w = _csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fieldnames})
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/admin/api/export/leads.csv", dependencies=[Depends(_check_bot404)])
async def export_leads(x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    rows = await pool.fetch(
        """SELECT id, session_id, name, phone, email, telegram, company, note, created_at
           FROM bot_404_leads WHERE tenant_id=$1 ORDER BY created_at DESC""",
        tid,
    )
    data = [dict(r) for r in rows]
    return _csv_response(data, "leads-export.csv",
                         ["id", "created_at", "name", "phone", "email", "telegram", "company", "session_id", "note"])


@app.get("/admin/api/export/dialogs.csv", dependencies=[Depends(_check_bot404)])
async def export_dialogs(x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    rows = await pool.fetch(
        """SELECT session_id, direction, text, created_at
           FROM bot_404_log WHERE tenant_id=$1 ORDER BY session_id, id""",
        tid,
    )
    data = [dict(r) for r in rows]
    return _csv_response(data, "dialogs-export.csv",
                         ["created_at", "session_id", "direction", "text"])


@app.get("/admin/api/export/usage.csv", dependencies=[Depends(_check_bot404)])
async def export_usage(days: int = 30, x_admin_token: str = Header(default="")):
    if days < 1 or days > 365:
        days = 30
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    rows = await pool.fetch(
        """SELECT ts, kind, model, in_tokens, out_tokens,
                  ROUND(cost_rub_x100 / 100.0, 4) AS cost_rub, latency_ms, request_id
           FROM usage_events
           WHERE tenant_id=$1 AND ts >= now() - ($2 || ' days')::interval
           ORDER BY ts DESC""",
        tid, str(days),
    )
    data = [dict(r) for r in rows]
    return _csv_response(data, f"usage-export-{days}d.csv",
                         ["ts", "kind", "model", "in_tokens", "out_tokens", "cost_rub", "latency_ms", "request_id"])


# ── Human takeover (оператор перехватывает сессию) ───────────────────────────


class _OperatorMsgBody(BaseModel):
    text: str


async def _assert_session_belongs_to_tenant(pool, session_id: str, tenant_id: int | None) -> None:
    """Проверяет, что session_id принадлежит этому тенанту (по bot_404_log).
    Бросает 403 если нет. Если сессия новая (нет ни одной записи) — разрешаем,
    привязка установится первой записью с tenant_id."""
    if not tenant_id:
        raise HTTPException(status_code=401, detail="tenant not resolved")
    row = await pool.fetchrow(
        "SELECT tenant_id FROM bot_404_log WHERE session_id=$1 LIMIT 1", session_id,
    )
    if row and row["tenant_id"] is not None and row["tenant_id"] != tenant_id:
        raise HTTPException(status_code=403, detail="session does not belong to your tenant")


@app.get("/admin/api/session/{session_id}/facts", dependencies=[Depends(_check_bot404)])
async def session_facts(session_id: str, x_admin_token: str = Header(default="")):
    """Возвращает структурированную память о клиенте (session_facts) для одной сессии.
    Защита tenant — только если сессия принадлежит этому tenant."""
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    await _assert_session_belongs_to_tenant(pool, session_id, tid)
    row = await pool.fetchrow(
        """SELECT industry, team_size, volume_per_day, avg_check_rub::float AS avg_check_rub,
                  current_crm, current_telephony, budget_rub::float AS budget_rub,
                  decision_role, product_interest, demo_promised,
                  contact_email, contact_phone, contact_telegram,
                  mentioned_pains, mentioned_objections,
                  last_summary, last_summary_at, updated_at
           FROM bot_404_session_facts WHERE session_id=$1 AND tenant_id=$2""",
        session_id, tid,
    )
    if not row:
        return {"session_id": session_id, "facts": None}
    return {"session_id": session_id, "facts": dict(row)}


@app.post("/admin/api/session/{session_id}/takeover", dependencies=[Depends(_check_bot404)])
async def session_takeover(session_id: str, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    await _assert_session_belongs_to_tenant(pool, session_id, tid)
    uid = await pool.fetchval(
        "SELECT id FROM tenant_users WHERE tenant_id=$1 ORDER BY id LIMIT 1", tid,
    )
    await pool.execute(
        """INSERT INTO session_meta(session_id, tenant_id, human_takeover, taken_by_user_id, taken_at)
           VALUES($1, $2, true, $3, now())
           ON CONFLICT (session_id) DO UPDATE SET
             human_takeover=true, taken_by_user_id=EXCLUDED.taken_by_user_id, taken_at=now(), updated_at=now()""",
        session_id, tid, uid,
    )
    return {"ok": True, "human_takeover": True}


@app.post("/admin/api/session/{session_id}/release", dependencies=[Depends(_check_bot404)])
async def session_release(session_id: str, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    await _assert_session_belongs_to_tenant(pool, session_id, tid)
    await pool.execute(
        """UPDATE session_meta SET human_takeover=false, taken_by_user_id=NULL, taken_at=NULL, updated_at=now()
           WHERE session_id=$1 AND tenant_id=$2""",
        session_id, tid,
    )
    return {"ok": True, "human_takeover": False}


@app.post("/admin/api/session/{session_id}/operator-message", dependencies=[Depends(_check_bot404)])
async def session_operator_message(session_id: str, body: _OperatorMsgBody, x_admin_token: str = Header(default="")):
    """Ручное сообщение от оператора. Для TG — отправляется в чат через bot-server,
    для виджета — пишется только в bot_404_log (виджет подтянет polling-ом).
    Bot-server вызывается через docker network."""
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    await _assert_session_belongs_to_tenant(pool, session_id, tid)
    await pool.execute(
        "INSERT INTO bot_404_log(session_id, direction, text, tenant_id) VALUES($1, 'out', $2, $3)",
        session_id, text, tid,
    )
    sent_to_tg = False
    if session_id.startswith("tg:"):
        if not _INTERNAL_SECRET:
            print("[operator-message] INTERNAL_API_SECRET not set — TG send skipped")
        else:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=5.0) as c:
                    r = await c.post(
                        "http://bot404:8090/api/internal/tg-send",
                        json={"session_id": session_id, "text": text},
                        headers={"X-Internal-Secret": _INTERNAL_SECRET},
                    )
                    sent_to_tg = r.status_code == 200
            except Exception as e:
                print(f"[operator-message] tg-send failed: {e}")
    return {"ok": True, "sent_to_tg": sent_to_tg}


# ── Партнёрская программа (ручной учёт) ─────────────────────────────────────


class _PartnerBody(BaseModel):
    name: str
    contact: str
    contact_type: str = "email"  # email | telegram | phone
    referral_code: str | None = None
    rate_pct: float = 10.0
    status: str = "active"
    note: str | None = None


class _PartnerLeadBody(BaseModel):
    lead_name: str
    status: str = "new"  # new | in_progress | deal | rejected
    reward_rub: float = 0
    payout_status: str = "pending"  # pending | paid
    note: str | None = None


@app.get("/admin/api/partners", dependencies=[Depends(_check_bot404)])
async def list_partners(x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    rows = await pool.fetch(
        """SELECT p.id, p.name, p.contact, p.contact_type, p.referral_code,
                  p.rate_pct::float AS rate_pct, p.status, p.note, p.joined_at,
                  COALESCE(s.leads_count, 0)::int AS leads_count,
                  COALESCE(s.deals_count, 0)::int AS deals_count,
                  COALESCE(s.total_reward, 0)::float AS total_reward,
                  COALESCE(s.pending_reward, 0)::float AS pending_reward
           FROM bot_404_partners p
           LEFT JOIN (
             SELECT partner_id,
                    COUNT(*) AS leads_count,
                    COUNT(*) FILTER (WHERE status='deal') AS deals_count,
                    SUM(reward_rub) AS total_reward,
                    SUM(CASE WHEN payout_status='pending' THEN reward_rub ELSE 0 END) AS pending_reward
             FROM bot_404_partner_leads GROUP BY partner_id
           ) s ON s.partner_id = p.id
           WHERE p.tenant_id = $1
           ORDER BY p.joined_at DESC""",
        tid,
    )
    return {"partners": [dict(r) for r in rows]}


@app.post("/admin/api/partners", dependencies=[Depends(_check_bot404)])
async def create_partner(body: _PartnerBody, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    row = await pool.fetchrow(
        """INSERT INTO bot_404_partners(tenant_id, name, contact, contact_type, referral_code, rate_pct, status, note)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
        tid, body.name, body.contact, body.contact_type, body.referral_code,
        body.rate_pct, body.status, body.note,
    )
    return {"id": row["id"]}


@app.patch("/admin/api/partners/{partner_id}", dependencies=[Depends(_check_bot404)])
async def update_partner(partner_id: int, body: _PartnerBody, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    await pool.execute(
        """UPDATE bot_404_partners
           SET name=$1, contact=$2, contact_type=$3, referral_code=$4,
               rate_pct=$5, status=$6, note=$7
           WHERE id=$8 AND tenant_id=$9""",
        body.name, body.contact, body.contact_type, body.referral_code,
        body.rate_pct, body.status, body.note, partner_id, tid,
    )
    return {"ok": True}


@app.delete("/admin/api/partners/{partner_id}", dependencies=[Depends(_check_bot404)])
async def delete_partner(partner_id: int, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    await pool.execute("DELETE FROM bot_404_partners WHERE id=$1 AND tenant_id=$2", partner_id, tid)
    return {"ok": True}


@app.get("/admin/api/partners/{partner_id}/leads", dependencies=[Depends(_check_bot404)])
async def list_partner_leads(partner_id: int, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    rows = await pool.fetch(
        """SELECT id, lead_name, status, reward_rub::float AS reward_rub,
                  payout_status, note, created_at, closed_at
           FROM bot_404_partner_leads
           WHERE partner_id=$1 AND tenant_id=$2
           ORDER BY created_at DESC""",
        partner_id, tid,
    )
    return {"leads": [dict(r) for r in rows]}


@app.post("/admin/api/partners/{partner_id}/leads", dependencies=[Depends(_check_bot404)])
async def create_partner_lead(partner_id: int, body: _PartnerLeadBody, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    closed_at = "now()" if body.status in ("deal", "rejected") else "NULL"
    row = await pool.fetchrow(
        f"""INSERT INTO bot_404_partner_leads(partner_id, tenant_id, lead_name, status, reward_rub, payout_status, note, closed_at)
            VALUES($1,$2,$3,$4,$5,$6,$7, {closed_at}) RETURNING id""",
        partner_id, tid, body.lead_name, body.status, body.reward_rub,
        body.payout_status, body.note,
    )
    return {"id": row["id"]}


@app.patch("/admin/api/partner-leads/{lead_id}", dependencies=[Depends(_check_bot404)])
async def update_partner_lead(lead_id: int, body: _PartnerLeadBody, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    closed_clause = ", closed_at=now()" if body.status in ("deal", "rejected") else ", closed_at=NULL"
    await pool.execute(
        f"""UPDATE bot_404_partner_leads
            SET lead_name=$1, status=$2, reward_rub=$3, payout_status=$4, note=$5 {closed_clause}
            WHERE id=$6 AND tenant_id=$7""",
        body.lead_name, body.status, body.reward_rub, body.payout_status,
        body.note, lead_id, tid,
    )
    return {"ok": True}


@app.delete("/admin/api/partner-leads/{lead_id}", dependencies=[Depends(_check_bot404)])
async def delete_partner_lead(lead_id: int, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    await pool.execute("DELETE FROM bot_404_partner_leads WHERE id=$1 AND tenant_id=$2", lead_id, tid)
    return {"ok": True}


# ── Internal: регистрация партнёра из бота ────────────────────────────────────

class _PartnerRegisterBody(BaseModel):
    name: str
    contact: str
    contact_type: str = "telegram"
    session_id: str | None = None
    source: str | None = None  # 'tg-bot' | 'widget'
    tenant_slug: str | None = None  # тенант бота; дефолт aisha (единственный с партнёркой)


@app.post("/internal/partner-register", dependencies=[Depends(_check_internal)])
async def internal_partner_register(body: _PartnerRegisterBody):
    """Создаёт заявку партнёра со status='pending'. Если контакт уже есть — возвращает существующего."""
    pool = await _b404_pool()
    # internal-эндпоинт без admin-токена: тенант берём из body (дефолт aisha), НЕ из несуществующего x_admin_token
    tid = await pool.fetchval("SELECT id FROM tenants WHERE slug=$1", body.tenant_slug or "aisha")
    existing = await pool.fetchrow(
        "SELECT id, status FROM bot_404_partners WHERE tenant_id=$1 AND lower(contact)=lower($2) LIMIT 1",
        tid, body.contact,
    )
    if existing:
        return {
            "ok": True, "id": existing["id"], "status": existing["status"],
            "already_exists": True,
        }
    note = f"Самостоятельная регистрация через {body.source or 'bot'}"
    if body.session_id:
        note += f" (session={body.session_id})"
    row = await pool.fetchrow(
        """INSERT INTO bot_404_partners(tenant_id, name, contact, contact_type, status, rate_pct, note)
           VALUES($1, $2, $3, $4, 'pending', 10.0, $5) RETURNING id""",
        tid, body.name, body.contact, body.contact_type, note,
    )
    return {"ok": True, "id": row["id"], "status": "pending", "already_exists": False}


@app.post("/admin/api/partners/{partner_id}/approve", dependencies=[Depends(_check_bot404)])
async def approve_partner(partner_id: int, x_admin_token: str = Header(default="")):
    """Одобрить заявку: status → active, генерим реф-код если ещё нет."""
    import secrets as _sec
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    p = await pool.fetchrow("SELECT id, name, referral_code FROM bot_404_partners WHERE id=$1 AND tenant_id=$2", partner_id, tid)
    if not p:
        raise HTTPException(status_code=404, detail="not found")
    code = p["referral_code"] or (p["name"][:4].upper().translate({ord(c): None for c in " .,-_@"}) + _sec.token_hex(2).upper())[:8]
    await pool.execute(
        "UPDATE bot_404_partners SET status='active', referral_code=$1 WHERE id=$2 AND tenant_id=$3",
        code, partner_id, tid,
    )
    return {"ok": True, "referral_code": code}


@app.post("/admin/api/partners/{partner_id}/reject", dependencies=[Depends(_check_bot404)])
async def reject_partner(partner_id: int, x_admin_token: str = Header(default="")):
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
    await pool.execute(
        "UPDATE bot_404_partners SET status='banned' WHERE id=$1 AND tenant_id=$2",
        partner_id, tid,
    )
    return {"ok": True}


# ── Internal: статистика партнёра для bot-server (TG) ────────────────────────

@app.get("/internal/partner-stats", dependencies=[Depends(_check_internal)])
async def internal_partner_stats(
    contact: str | None = None,
    session_id: str | None = None,
    message: str | None = None,
    tenant_slug: str = "aisha",
):
    """Идентификация партнёра + его статистика. Зовёт bot-server по docker network.
    Параметры (любая комбинация):
      contact     — явный @username/email
      session_id  — для tg-сессий резолвим username из bot_404_tg_users
      message     — текст, ищем @user/email в тексте"""
    # Inline helpers (раньше жили в agents.orchestrator)
    import re as _re_inline
    import psycopg2.extras as _pg_extras
    from knowledge.vector_store import _get_conn as _kb_conn
    _TG_RX = _re_inline.compile(r"@([A-Za-z0-9_]{3,32})")
    _EMAIL_RX = _re_inline.compile(r"\b([\w.+-]+@[\w-]+\.[\w.-]+)\b")

    def _resolve_partner_contact(session_id, msg):
        em = _EMAIL_RX.search(msg or "")
        if em: return (em.group(1).lower(), "explicit")
        tg = _TG_RX.search(msg or "")
        if tg: return ("@" + tg.group(1).lower(), "explicit")
        if not session_id or not session_id.startswith("tg:"): return None
        try:
            conn = _kb_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT username FROM bot_404_tg_users WHERE session_id=%s AND username IS NOT NULL LIMIT 1", (session_id,))
                row = cur.fetchone(); u = row[0] if row else None
        except Exception:
            u = None
        return ("@" + str(u).lower(), "tg-session") if u else None

    def get_partner_stats(contact_val, p_tid):
        try:
            conn = _kb_conn()
            with conn.cursor(cursor_factory=_pg_extras.RealDictCursor) as cur:
                cur.execute("""SELECT id, name, contact, rate_pct::float AS rate_pct, status, joined_at, referral_code
                               FROM bot_404_partners WHERE lower(contact)=lower(%s) AND tenant_id=%s LIMIT 1""", (contact_val, p_tid))
                p = cur.fetchone()
                if not p: return None
                cur.execute("""SELECT lead_name, status, reward_rub::float AS reward_rub, payout_status, created_at
                               FROM bot_404_partner_leads WHERE partner_id=%s AND tenant_id=%s ORDER BY created_at DESC LIMIT 50""", (p["id"], p_tid))
                leads = [dict(r) for r in cur.fetchall()]
        except Exception as e:
            print(f"[partner-stats] db failed: {e}"); return None
        p = dict(p)
        totals = {
            "leads_count": len(leads),
            "deals_count": sum(1 for l in leads if l["status"] == "deal"),
            "in_progress_count": sum(1 for l in leads if l["status"] == "in_progress"),
            "total_reward": sum(float(l["reward_rub"] or 0) for l in leads),
            "pending_reward": sum(float(l["reward_rub"] or 0) for l in leads if l["payout_status"] == "pending"),
            "paid_reward": sum(float(l["reward_rub"] or 0) for l in leads if l["payout_status"] == "paid"),
        }
        if p.get("joined_at"): p["joined_at"] = p["joined_at"].isoformat()
        for l in leads:
            if l.get("created_at"): l["created_at"] = l["created_at"].isoformat()
        return {"partner": p, "leads": leads, "totals": totals}

    resolved_contact = contact
    source = "explicit-arg" if contact else None
    if not resolved_contact:
        r = _resolve_partner_contact(session_id, message or "")
        if r:
            resolved_contact, source = r
    if not resolved_contact:
        return {"found": False, "reason": "contact_not_resolved"}
    # tenant бота (дефолт aisha): партнёра ищем ТОЛЬКО в пределах этого тенанта (изоляция)
    _p_tid = None
    try:
        _c = _kb_conn()
        with _c.cursor() as _cur:
            _cur.execute("SELECT id FROM tenants WHERE slug=%s", (tenant_slug,)); _r = _cur.fetchone()
            _p_tid = _r[0] if _r else None
    except Exception:
        _p_tid = None
    if _p_tid is None:
        return {"found": False, "reason": "tenant_not_resolved"}
    stats = get_partner_stats(resolved_contact, _p_tid)
    if not stats:
        return {"found": False, "reason": "not_a_partner", "contact": resolved_contact, "source": source}
    return {"found": True, "contact": resolved_contact, "source": source, **stats}


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
