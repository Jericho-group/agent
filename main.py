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

    # Загружаем Telegram-аккаунты и подписываемся на входящие
    tg_client.set_incoming_handler(campaign_manager.handle_incoming)
    await tg_client.load_active_accounts()
    print("[startup] Proactive TG module ready")
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
])


@app.middleware("http")
async def _admin_cors_guard(request: Request, call_next):
    """Для /admin/api/* — реальный CORS-фильтр по Origin. Если запрос пришёл с браузерного
    Origin, который не в whitelist, отвечаем 403. Server-to-server (без Origin) — пропускаем."""
    path = request.url.path or ""
    if path.startswith("/admin/api/") or path.startswith("/admin/ingest"):
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
                    result["tenants"] = await con.fetchval("SELECT COUNT(*) FROM tenants WHERE is_active=true")
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


def _check_bot404(x_admin_token: str = Header(default="")):
    # Принимаем JWT либо legacy global-токены
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Missing X-Admin-Token")
    claims = _decode_jwt(x_admin_token)
    if claims:
        return  # JWT валиден
    if (settings.admin_token and _secrets_mod.compare_digest(x_admin_token, settings.admin_token)) \
       or (settings.bot404_token and _secrets_mod.compare_digest(x_admin_token, settings.bot404_token)):
        return  # Legacy токен
    raise HTTPException(status_code=401, detail="Invalid admin token")


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


@app.post("/admin/api/team", dependencies=[Depends(_check_bot404)])
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
    await _audit("team.add", {"id": uid, "email": body.email, "role": body.role})
    return {"ok": True, "id": uid}


@app.delete("/admin/api/team/{user_id}", dependencies=[Depends(_check_bot404)])
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
    await _audit("team.delete", {"id": user_id})
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
    # company — из брендинга тенанта, а не хардкод 404ai (иначе клиент видит вендора в своём кабинете)
    brand = await pool.fetchval("SELECT COALESCE(brand_name, name) FROM v_tenant_branding WHERE tenant_id=$1", tid) or ""
    bot_nm = await pool.fetchval("SELECT bot_name FROM v_tenant_branding WHERE tenant_id=$1", tid) or ""
    company = (str(brand) + (" · " + str(bot_nm) if bot_nm else "")).strip(" ·") or "—"
    return {
        "stats": {"status": "online", "active_sessions": sessions, "knowledge_base_docs": 0,
                  "models": {"agent": "gemini-2.5-flash-lite", "router": "gemini-2.5-flash (критик)", "orchestrator": "gemini-2.5-flash"},
                  "company": company},
        "extStats": {"total_sessions": sessions, "total_messages": total, "messages_24h": last24,
                     "qualified_leads": leads, "escalated": 0, "temperature": {"hot": 0, "warm": 0, "cold": 0}, "intents": []},
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


@app.post("/admin/api/knowledge/upload", dependencies=[Depends(_check_bot404)])
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
        doc_id = str(item.get("id") or f"upload-{tid}-{inserted}")
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
                     tenant_id=EXCLUDED.tenant_id, updated_at=now()""",
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
    conditions = ["(tenant_id = %s OR tenant_id IS NULL)"]
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


@app.delete("/admin/api/knowledge", dependencies=[Depends(_check_bot404)])
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


@app.post("/admin/api/proactive/accounts/request-code", dependencies=[Depends(_check_bot404)])
async def proactive_request_code(req: TgCodeRequest):
    try:
        result = await tg_client.request_code(req.phone)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/admin/api/proactive/accounts/confirm-code", dependencies=[Depends(_check_bot404)])
async def proactive_confirm_code(req: TgConfirmRequest, x_admin_token: str = Header(default="")):
    try:
        tid = await _tenant_id_from_token(x_admin_token)
        await tg_client.confirm_code(req.phone, req.code, req.phone_code_hash, tid)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/admin/api/proactive/campaigns", dependencies=[Depends(_check_bot404)])
async def proactive_list_campaigns(x_admin_token: str = Header(default="")):
    tid = await _tenant_id_from_token(x_admin_token)
    return await campaign_manager.list_campaigns(tid)


@app.post("/admin/api/proactive/campaigns", dependencies=[Depends(_check_bot404)])
async def proactive_create_campaign(req: CampaignCreate, x_admin_token: str = Header(default="")):
    tid = await _tenant_id_from_token(x_admin_token)
    try:
        return await campaign_manager.create_campaign(req.model_dump(), tid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/admin/api/proactive/campaigns/{campaign_id}", dependencies=[Depends(_check_bot404)])
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
async def _audit(action: str, payload: dict | None, slug: str = "aisha", actor_email: str = "tenant"):
    try:
        pool = await _b404_pool()
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


@app.post("/admin/api/branding", dependencies=[Depends(_check_bot404)])
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
    await _audit("branding.update", {k: v for k, v in data.items() if v is not None})
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


@app.post("/admin/api/tg-bots", dependencies=[Depends(_check_bot404)])
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
    await _audit("tg_bot.add", {"id": bot_id, "username": me.get("username")})
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
    await _audit("tg_bot.toggle", {"id": bot_id, "enabled": new_state})
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
    await _audit("tg_bot.delete", {"id": bot_id})
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


@app.post("/internal/partner-register", dependencies=[Depends(_check_internal)])
async def internal_partner_register(body: _PartnerRegisterBody):
    """Создаёт заявку партнёра со status='pending'. Если контакт уже есть — возвращает существующего."""
    pool = await _b404_pool()
    tid = await _tenant_id_from_token(x_admin_token)
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

    def get_partner_stats(contact_val):
        try:
            conn = _kb_conn()
            with conn.cursor(cursor_factory=_pg_extras.RealDictCursor) as cur:
                cur.execute("""SELECT id, name, contact, rate_pct::float AS rate_pct, status, joined_at, referral_code
                               FROM bot_404_partners WHERE lower(contact)=lower(%s) LIMIT 1""", (contact_val,))
                p = cur.fetchone()
                if not p: return None
                cur.execute("""SELECT lead_name, status, reward_rub::float AS reward_rub, payout_status, created_at
                               FROM bot_404_partner_leads WHERE partner_id=%s ORDER BY created_at DESC LIMIT 50""", (p["id"],))
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
    stats = get_partner_stats(resolved_contact)
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
