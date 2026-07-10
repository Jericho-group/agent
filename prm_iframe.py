"""PRM iframe SSO — API-модуль для встраивания ЛК Дирижёра в PRM-платформы партнёров.

По ТЗ TZ_PRM_iframe_SSO-2.docx. Реализует три обмена:
  1. /prm/api/partners        — CRUD sub-тенантов партнёра
  2. /prm/api/sso-token       — одноразовый JWT для открытия iframe
  3. /embed                   — точка входа: валидация JWT → cookie → редирект в ЛК

Аутентификация /prm/api/*: заголовок Authorization: Bearer prm_<hex>
Аутентификация /embed:     одноразовый JWT в query (?t=...)

Не путать с существующей рефералкой Аиши (bot_404_partners) — это разные подсистемы.
"""

from __future__ import annotations

import os
import secrets as _secrets
import time as _time_mod
import uuid as _uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr

try:
    import jwt as _jwt
except ImportError:
    _jwt = None

try:
    import bcrypt as _bcrypt
except ImportError:
    _bcrypt = None


# ── Конфигурация ────────────────────────────────────────────────────────────
# Отдельный секрет для подписи SSO-JWT (не смешивается с админским JWT_SECRET).
# Fallback на JWT_SECRET при бутстрапе, но лучше задать явно в prod .env.
_PRM_JWT_SECRET = os.environ.get("PRM_JWT_SECRET") or os.environ.get("JWT_SECRET") or ""
_PRM_JWT_ALG = "HS256"
_PRM_SSO_TTL = 60                          # ТЗ раздел 5.1: одноразовый токен 60 сек
_PRM_SESSION_TTL = 2 * 3600                # ТЗ раздел 8.2: сессионная cookie 2 часа
_SESSION_COOKIE_NAME = "orchestra_sess"    # ТЗ раздел 8.2

# База URL, куда мы указываем Партнёру в ответе /prm/api/sso-token
# ({url: "https://<host>/embed?t=..."}). Из env, чтобы работало на prod / test / dev.
_PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "https://admin.dirizher404.ru").rstrip("/")


router = APIRouter(prefix="/prm/api", tags=["prm-iframe"])
embed_router = APIRouter(tags=["prm-embed"])


# ── CSP middleware: frame-ancestors динамически из prm_partners.allowed_origins ──
# Кэш partner_id → allowed_origins с TTL 30 сек, чтобы не бить в БД на каждый запрос.
_allowed_origins_cache: dict[int, tuple[float, list[str]]] = {}
_ALLOWED_ORIGINS_TTL = 30.0


async def _get_partner_allowed_origins(partner_id: int) -> list[str]:
    now = _time_mod.time()
    cached = _allowed_origins_cache.get(partner_id)
    if cached and cached[0] > now:
        return cached[1]
    pool = await _get_pool()
    origins = await pool.fetchval(
        "SELECT allowed_origins FROM prm_partners WHERE id=$1", partner_id
    ) or []
    _allowed_origins_cache[partner_id] = (now + _ALLOWED_ORIGINS_TTL, list(origins))
    return list(origins)


async def prm_csp_middleware(request, call_next):
    """Ставит Content-Security-Policy: frame-ancestors для страниц, которые могут быть в iframe.

    Скоуп: /embed и /admin* (включая статику /admin/index.html и т.д.). Для остального
    приложения — не трогаем существующие CSP-заголовки (если есть).

    Резолв allowed_origins:
    - /embed?t=JWT: декодировать JWT (без валидации подписи — только для чтения claims),
      достать prm_partner из payload → allowed_origins этого партнёра.
    - /admin*: смотрим cookie orchestra_sess, если есть → декодируем session JWT,
      получаем tenant_id → JOIN parent_prm_partner_id → allowed_origins.
      Если куки нет — CSP: frame-ancestors 'none' (значит НЕ iframe-сессия,
      обычный ЛК не даём встраивать никуда).
    """
    response = await call_next(request)
    path = request.url.path

    if not (path.startswith("/embed") or path.startswith("/admin")):
        return response

    origins: list[str] = []
    try:
        if path.startswith("/embed"):
            token = request.query_params.get("t") or ""
            if token and _jwt:
                # Читаем claims без валидации подписи — только чтобы достать prm_partner.
                # Подпись валидируется уже в handler'е.
                claims = _jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
                prm_partner = claims.get("prm_partner")
                if prm_partner:
                    origins = await _get_partner_allowed_origins(int(prm_partner))
        elif path.startswith("/admin"):
            cookie = request.cookies.get(_SESSION_COOKIE_NAME) or ""
            if cookie and _jwt:
                claims = _jwt.decode(cookie, options={"verify_signature": False, "verify_aud": False})
                tid = claims.get("tid")
                if tid:
                    pool = await _get_pool()
                    partner_id = await pool.fetchval(
                        "SELECT parent_prm_partner_id FROM tenants WHERE id=$1", int(tid)
                    )
                    if partner_id:
                        origins = await _get_partner_allowed_origins(int(partner_id))
    except Exception as e:
        # Не ломаем ответ — просто без CSP-headers (или c 'none')
        print(f"[prm-csp] resolve fail: {e}")
        origins = []

    if origins:
        # frame-ancestors 'self' + список — 'self' нужен чтобы могли открывать напрямую
        # (без iframe) для отладки и разработки.
        csp = "frame-ancestors 'self' " + " ".join(origins) + ";"
    else:
        # Не embed-сессия / нет привязки к партнёру → не даём встраивать.
        csp = "frame-ancestors 'self';"
    # Не перезатираем существующие CSP-директивы (если middleware выше уже что-то поставил).
    existing = response.headers.get("Content-Security-Policy", "")
    if "frame-ancestors" not in existing:
        response.headers["Content-Security-Policy"] = (existing + " " + csp).strip() if existing else csp
    return response


# ── Импорт пула БД: без циклической зависимости от main.py ──────────────────
# _b404_pool() возвращает asyncpg pool; используется по всему main.py.
def _get_pool():
    from memory.dialogue_memory import _get_pool as _b404_pool
    return _b404_pool()


# ── Утилиты ─────────────────────────────────────────────────────────────────
def _gen_api_key() -> tuple[str, str]:
    """Возвращает (полный_ключ, префикс_для_lookup).

    Формат: prm_<32 hex> = 36 символов.
    Префикс: prm_<первые_8_hex> = 12 символов (индексируется, не является секретом).
    """
    hex_body = _secrets.token_hex(16)  # 32 hex-символа
    full = "prm_" + hex_body
    prefix = "prm_" + hex_body[:8]
    return full, prefix


def _hash_key(full_key: str) -> str:
    if not _bcrypt:
        raise HTTPException(status_code=503, detail="bcrypt not installed")
    return _bcrypt.hashpw(full_key.encode("utf-8")[:72], _bcrypt.gensalt()).decode("utf-8")


def _check_key(full_key: str, hashed: str) -> bool:
    if not _bcrypt:
        return False
    try:
        return _bcrypt.checkpw(full_key.encode("utf-8")[:72], hashed.encode("utf-8"))
    except Exception:
        return False


async def _audit_prm(
    action: str,
    *,
    partner_id: int | None = None,
    target_tenant_id: int | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    status: str = "success",
    error_code: str | None = None,
    meta: dict | None = None,
) -> None:
    """Пишет строку в prm_audit_log. Никогда не поднимает исключение (best-effort)."""
    try:
        pool = await _get_pool()
        await pool.execute(
            """INSERT INTO prm_audit_log
               (partner_id, action, target_tenant_id, ip, user_agent, request_id, status, error_code, meta)
               VALUES ($1, $2, $3, $4::inet, $5, $6::uuid, $7, $8, $9::jsonb)""",
            partner_id, action, target_tenant_id, ip, user_agent,
            request_id, status, error_code, __import__("json").dumps(meta or {}),
        )
    except Exception as e:
        print(f"[prm-audit] fail: {e}")  # не блокируем flow


# ── Аутентификация партнёра по API-ключу ────────────────────────────────────
async def _check_prm_partner(
    request: Request,
    authorization: str = Header(default=""),
) -> dict:
    """Валидирует Authorization: Bearer prm_<hex> и возвращает {partner_id, name, status}.

    401 — нет заголовка / не тот формат / ключ не найден / bcrypt не сошёлся.
    403 — партнёр статус paused/revoked.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization: Bearer <api_key>")
    raw = authorization[7:].strip()
    if not raw.startswith("prm_") or len(raw) != 36:
        raise HTTPException(status_code=401, detail="Invalid API key format")
    prefix = "prm_" + raw[4:12]

    pool = await _get_pool()
    row = await pool.fetchrow(
        """SELECT k.id AS key_id, k.partner_id, k.key_hash, k.status AS key_status,
                  p.name, p.status AS partner_status
             FROM prm_partner_api_keys k
             JOIN prm_partners p ON p.id = k.partner_id
            WHERE k.key_prefix = $1
              AND k.status IN ('active', 'rotating')""",
        prefix,
    )
    if not row or not _check_key(raw, row["key_hash"]):
        # маскируем ошибку — не подсказываем что не так
        await _audit_prm("auth.fail", ip=(request.client.host if request.client else None),
                         status="error", error_code="bad_key",
                         meta={"prefix": prefix})
        raise HTTPException(status_code=401, detail="Invalid API key")
    if row["partner_status"] != "active":
        await _audit_prm("auth.blocked", partner_id=row["partner_id"],
                         ip=(request.client.host if request.client else None),
                         status="error", error_code="partner_" + row["partner_status"])
        raise HTTPException(status_code=403, detail=f"Partner is {row['partner_status']}")

    # last_used_at обновляем best-effort, не блокируя запрос
    try:
        await pool.execute(
            "UPDATE prm_partner_api_keys SET last_used_at = now() WHERE id = $1",
            row["key_id"],
        )
    except Exception:
        pass

    return {
        "partner_id": row["partner_id"],
        "name": row["name"],
        "key_id": row["key_id"],
    }


# ── Схемы запросов/ответов ──────────────────────────────────────────────────
class _CreatePartnerBody(BaseModel):
    name: str
    email: str
    external_id: Optional[str] = None


class _PatchPartnerBody(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    external_id: Optional[str] = None


class _SsoTokenBody(BaseModel):
    partner_id: int   # В контексте API это tenants.id (терминология ТЗ)


# ── Обмен №2: заведение конечного пользователя (создать sub-тенанта) ────────
@router.post("/partners")
async def prm_create_partner(
    body: _CreatePartnerBody,
    request: Request,
    partner=Depends(_check_prm_partner),
):
    """POST /prm/api/partners — создаёт sub-тенанта под текущим PRM-партнёром.

    Body: {name, email, external_id?}
    Response: {partner_id (=tenants.id), created_at}
    """
    if not body.name or not body.email:
        raise HTTPException(status_code=400, detail="name and email required")

    pool = await _get_pool()
    prm_partner_id = partner["partner_id"]

    # Дефолт-план: берём из prm_partners.default_plan_id либо fallback trial (id=1)
    default_plan = await pool.fetchval(
        "SELECT COALESCE(default_plan_id, (SELECT id FROM plans WHERE code='trial')) "
        "FROM prm_partners WHERE id=$1",
        prm_partner_id,
    )

    # external_id должен быть уникален в рамках PRM-партнёра
    if body.external_id:
        exists = await pool.fetchrow(
            "SELECT id FROM tenants WHERE parent_prm_partner_id=$1 AND prm_external_id=$2",
            prm_partner_id, body.external_id,
        )
        if exists:
            await _audit_prm(
                "partner.create_conflict", partner_id=prm_partner_id,
                target_tenant_id=exists["id"],
                ip=(request.client.host if request.client else None),
                status="error", error_code="duplicate",
                meta={"external_id": body.external_id},
            )
            raise HTTPException(status_code=409, detail=f"Tenant with external_id={body.external_id} already exists")

    # Слаг: prm-<partner_id>-<uuid8>. Гарантированно уникален.
    slug = f"prm-{prm_partner_id}-{_uuid.uuid4().hex[:8]}"

    tid = await pool.fetchval(
        """INSERT INTO tenants (slug, name, plan_id, contact_email,
                                parent_prm_partner_id, prm_external_id)
           VALUES ($1, $2, $3, $4, $5, $6)
           RETURNING id""",
        slug, body.name, default_plan, body.email,
        prm_partner_id, body.external_id,
    )
    # created_at — берём из БД для точности
    created_at = await pool.fetchval("SELECT created_at FROM tenants WHERE id=$1", tid)

    await _audit_prm(
        "partner.create", partner_id=prm_partner_id, target_tenant_id=tid,
        ip=(request.client.host if request.client else None),
        meta={"name": body.name, "external_id": body.external_id, "slug": slug},
    )
    return {"partner_id": tid, "created_at": created_at.isoformat()}


@router.get("/partners/{tid}")
async def prm_get_partner(
    tid: int,
    request: Request,
    partner=Depends(_check_prm_partner),
):
    """GET /prm/api/partners/{tid} — сведения о sub-тенанте (только своём)."""
    pool = await _get_pool()
    row = await pool.fetchrow(
        """SELECT id, name, slug, contact_email AS email, prm_external_id,
                  enabled, created_at, updated_at
             FROM tenants
            WHERE id = $1 AND parent_prm_partner_id = $2""",
        tid, partner["partner_id"],
    )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "partner_id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "external_id": row["prm_external_id"],
        "status": "active" if row["enabled"] else "paused",
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


@router.patch("/partners/{tid}")
async def prm_patch_partner(
    tid: int,
    body: _PatchPartnerBody,
    request: Request,
    partner=Depends(_check_prm_partner),
):
    """PATCH /prm/api/partners/{tid} — обновить name / email / external_id."""
    pool = await _get_pool()
    exists = await pool.fetchrow(
        "SELECT id FROM tenants WHERE id=$1 AND parent_prm_partner_id=$2",
        tid, partner["partner_id"],
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Not found")

    fields, values, idx = [], [], 1
    if body.name is not None:
        fields.append(f"name = ${idx}"); values.append(body.name); idx += 1
    if body.email is not None:
        fields.append(f"contact_email = ${idx}"); values.append(body.email); idx += 1
    if body.external_id is not None:
        fields.append(f"prm_external_id = ${idx}"); values.append(body.external_id); idx += 1
    if not fields:
        return {"ok": True, "changed": 0}

    fields.append("updated_at = now()")
    values.append(tid); values.append(partner["partner_id"])
    sql = f"UPDATE tenants SET {', '.join(fields)} WHERE id=${idx} AND parent_prm_partner_id=${idx+1}"
    await pool.execute(sql, *values)
    await _audit_prm(
        "partner.update", partner_id=partner["partner_id"], target_tenant_id=tid,
        ip=(request.client.host if request.client else None),
        meta={k: v for k, v in body.dict(exclude_none=True).items()},
    )
    return {"ok": True}


@router.post("/partners/{tid}/pause")
async def prm_pause_partner(
    tid: int,
    request: Request,
    partner=Depends(_check_prm_partner),
):
    """Приостановка: enabled=false. Данные сохраняются, SSO-token → 404."""
    pool = await _get_pool()
    updated = await pool.execute(
        "UPDATE tenants SET enabled=false, updated_at=now() "
        "WHERE id=$1 AND parent_prm_partner_id=$2",
        tid, partner["partner_id"],
    )
    if updated.endswith(" 0"):
        raise HTTPException(status_code=404, detail="Not found")
    await _audit_prm("partner.pause", partner_id=partner["partner_id"],
                     target_tenant_id=tid,
                     ip=(request.client.host if request.client else None))
    return {"ok": True, "status": "paused"}


@router.post("/partners/{tid}/resume")
async def prm_resume_partner(
    tid: int,
    request: Request,
    partner=Depends(_check_prm_partner),
):
    """Возобновление: enabled=true."""
    pool = await _get_pool()
    updated = await pool.execute(
        "UPDATE tenants SET enabled=true, updated_at=now() "
        "WHERE id=$1 AND parent_prm_partner_id=$2",
        tid, partner["partner_id"],
    )
    if updated.endswith(" 0"):
        raise HTTPException(status_code=404, detail="Not found")
    await _audit_prm("partner.resume", partner_id=partner["partner_id"],
                     target_tenant_id=tid,
                     ip=(request.client.host if request.client else None))
    return {"ok": True, "status": "active"}


@router.delete("/partners/{tid}")
async def prm_delete_partner(
    tid: int,
    request: Request,
    partner=Depends(_check_prm_partner),
):
    """Отзыв доступа. По ТЗ раздел 9.2 — мягкое удаление (enabled=false),
    жёсткое удаление — отдельным согласованием. Пока делаем soft-delete."""
    pool = await _get_pool()
    updated = await pool.execute(
        "UPDATE tenants SET enabled=false, updated_at=now() "
        "WHERE id=$1 AND parent_prm_partner_id=$2",
        tid, partner["partner_id"],
    )
    if updated.endswith(" 0"):
        raise HTTPException(status_code=404, detail="Not found")
    await _audit_prm("partner.delete_soft", partner_id=partner["partner_id"],
                     target_tenant_id=tid,
                     ip=(request.client.host if request.client else None))
    return {"ok": True, "deleted": "soft"}


# ── Обмен №3: генерация одноразового SSO-JWT ────────────────────────────────
@router.post("/sso-token")
async def prm_sso_token(
    body: _SsoTokenBody,
    request: Request,
    partner=Depends(_check_prm_partner),
):
    """POST /prm/api/sso-token — генерит одноразовый JWT для открытия iframe.

    Body: {partner_id: int}   (tenants.id sub-тенанта, терминология ТЗ)
    Response: {url, expires_in, issued_at}
    """
    if not _jwt or not _PRM_JWT_SECRET:
        raise HTTPException(status_code=503, detail="JWT not configured")

    pool = await _get_pool()
    # Проверяем что sub-тенант принадлежит этому партнёру и активен
    row = await pool.fetchrow(
        "SELECT id, enabled FROM tenants WHERE id=$1 AND parent_prm_partner_id=$2",
        body.partner_id, partner["partner_id"],
    )
    if not row:
        await _audit_prm("sso.issue", partner_id=partner["partner_id"],
                         ip=(request.client.host if request.client else None),
                         status="error", error_code="not_found",
                         meta={"requested_tid": body.partner_id})
        raise HTTPException(status_code=404, detail="Partner not found")
    if not row["enabled"]:
        await _audit_prm("sso.issue", partner_id=partner["partner_id"],
                         target_tenant_id=body.partner_id,
                         ip=(request.client.host if request.client else None),
                         status="error", error_code="paused")
        raise HTTPException(status_code=404, detail="Partner is paused")

    now = int(_time_mod.time())
    jti = str(_uuid.uuid4())
    payload = {
        "sub": str(body.partner_id),
        "aud": "orchestra-embed",
        "iss": "dirijer-prm",
        "prm_partner": partner["partner_id"],
        "iat": now,
        "exp": now + _PRM_SSO_TTL,
        "jti": jti,
    }
    token = _jwt.encode(payload, _PRM_JWT_SECRET, algorithm=_PRM_JWT_ALG)

    await _audit_prm("sso.issue", partner_id=partner["partner_id"],
                     target_tenant_id=body.partner_id,
                     ip=(request.client.host if request.client else None),
                     meta={"jti": jti})

    return {
        "url": f"{_PUBLIC_BASE_URL}/embed?t={token}",
        "expires_in": _PRM_SSO_TTL,
        "issued_at": _time_mod.strftime("%Y-%m-%dT%H:%M:%SZ", _time_mod.gmtime(now)),
    }


# ── /embed: точка входа iframe ──────────────────────────────────────────────
@embed_router.get("/embed")
async def embed_entry(request: Request, t: str = Query(...)):
    """GET /embed?t=<jwt> — валидирует SSO-JWT, ставит session-cookie, редирект в ЛК.

    Валидация:
    1. Декодировать JWT (подпись, exp, aud)
    2. Проверить jti не использован (иначе — replay-attempt, 401 + аудит)
    3. Insert jti в prm_sso_jti_used
    4. Установить cookie orchestra_sess (HttpOnly; Secure; SameSite=None)
    5. 302 → /admin?embed=1
    """
    if not _jwt or not _PRM_JWT_SECRET:
        raise HTTPException(status_code=503, detail="JWT not configured")

    ip = (request.client.host if request.client else None)

    # 1. Decode + verify (audience, exp, signature)
    try:
        claims = _jwt.decode(
            t, _PRM_JWT_SECRET, algorithms=[_PRM_JWT_ALG],
            audience="orchestra-embed",
            options={"require": ["exp", "iat", "sub", "jti", "aud"]},
        )
    except Exception as e:
        await _audit_prm("embed.reject", ip=ip, status="error",
                         error_code="bad_token", meta={"err": str(e)[:100]})
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    tid = int(claims["sub"])
    jti = claims["jti"]
    prm_partner_id = claims.get("prm_partner")
    exp_ts = int(claims["exp"])

    pool = await _get_pool()

    # 2 + 3. Проверка+запись jti (атомарно через ON CONFLICT DO NOTHING)
    from datetime import datetime, timezone
    inserted = await pool.execute(
        """INSERT INTO prm_sso_jti_used (jti, partner_id, tenant_id, expires_at, ip)
           VALUES ($1::uuid, $2, $3, $4, $5::inet)
           ON CONFLICT (jti) DO NOTHING""",
        jti, prm_partner_id, tid,
        datetime.fromtimestamp(exp_ts, tz=timezone.utc), ip,
    )
    if inserted.endswith(" 0"):
        # replay — jti уже был использован
        await _audit_prm("sso.replay_blocked", partner_id=prm_partner_id,
                         target_tenant_id=tid, ip=ip,
                         status="error", error_code="replay",
                         meta={"jti": jti})
        raise HTTPException(status_code=401, detail="Token already used")

    # 4. Проверить что тенант всё ещё активен (могли поставить на паузу за 60 сек)
    enabled = await pool.fetchval("SELECT enabled FROM tenants WHERE id=$1", tid)
    if not enabled:
        await _audit_prm("embed.reject", partner_id=prm_partner_id,
                         target_tenant_id=tid, ip=ip,
                         status="error", error_code="tenant_paused")
        raise HTTPException(status_code=403, detail="Tenant is paused")

    # 5. Установить cookie с сессионным JWT (2 часа) + редирект
    # Внутри /admin работаем через обычный JWT (issue_jwt), чтобы существующая
    # ЛК-логика работала без переделки.
    from main import issue_jwt
    # роль member — минимальные права, viewer/member могут работать с ЛК партнёра
    session_jwt = issue_jwt(
        user_id=None, tenant_id=tid, role="member", scope="bot404",
        email=f"prm-{prm_partner_id}-{tid}@embed",
    )

    resp = RedirectResponse(url="/admin?embed=1", status_code=302)
    resp.set_cookie(
        _SESSION_COOKIE_NAME,
        session_jwt,
        max_age=_PRM_SESSION_TTL,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
    )
    await _audit_prm("embed.open", partner_id=prm_partner_id,
                     target_tenant_id=tid, ip=ip,
                     meta={"jti": jti})
    return resp


# ── /admin/api/whoami — фронт-подхват JWT из cookie при embed=1 ─────────────
whoami_router = APIRouter(tags=["prm-embed"])


@whoami_router.get("/admin/api/whoami")
async def whoami(request: Request):
    """Читает cookie orchestra_sess и возвращает JWT (для передачи фронту).

    Используется admin/index.html при embed-режиме: получить JWT и сохранить
    в state → дальше слать через X-Admin-Token как обычно.
    """
    cookie = request.cookies.get(_SESSION_COOKIE_NAME) or ""
    if not cookie:
        raise HTTPException(status_code=401, detail="No session")
    # Валидируем через существующий _decode_jwt
    from main import _decode_jwt
    claims = _decode_jwt(cookie)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return {
        "token": cookie,
        "tenant_id": claims.get("tid"),
        "role": claims.get("role"),
        "scope": claims.get("scope"),
        "embed": True,
    }
