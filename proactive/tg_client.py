"""
Telegram MTProto клиент на базе Telethon.
Управляет сессиями нескольких аккаунтов.
"""
from __future__ import annotations

import asyncio
import base64
import os
from typing import Callable

import asyncpg
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from config import settings


# AES-GCM шифрование session_str (тем же ключом, что bot_token_enc — переменная BOT_TOKEN_ENC_KEY).
def _enc_key() -> bytes | None:
    k = os.environ.get("BOT_TOKEN_ENC_KEY", "")
    if not k:
        return None
    try:
        b = base64.urlsafe_b64decode(k + "=" * (-len(k) % 4))
        if len(b) == 32:
            return b
    except Exception:
        pass
    return None


def _encrypt_session(plain: str) -> str | None:
    key = _enc_key()
    if not key or not plain:
        return None
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes = AESGCM(key)
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, plain.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def _decrypt_session(token: str) -> str | None:
    key = _enc_key()
    if not key or not token:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if len(raw) < 13:
            return None
        nonce, ct = raw[:12], raw[12:]
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except Exception as e:
        print(f"[tg-client] decrypt session failed: {e}")
        return None

# Telegram API credentials — читаем через pydantic settings (из .env)
TG_API_ID   = settings.tg_api_id
TG_API_HASH = settings.tg_api_hash

# phone → TelegramClient
_clients: dict[str, TelegramClient] = {}
# phone → account_id в БД
_account_ids: dict[str, int] = {}
# phone → { user_id → input_entity }  — кэш для отправки ответов
_entity_cache: dict[str, dict[int, object]] = {}

# колбэк вызывается при входящем сообщении: (phone, sender_username, sender_id, text)
_on_incoming: Callable | None = None


def set_incoming_handler(fn: Callable):
    global _on_incoming
    _on_incoming = fn


async def _get_db() -> asyncpg.Connection:
    return await asyncpg.connect(settings.supabase_db_url)


def _make_handler(phone: str, client: TelegramClient):
    """Создаёт обработчик входящих сообщений для данного клиента."""
    @client.on(events.NewMessage(incoming=True))
    async def _handler(event):
        if not _on_incoming or not event.is_private:
            return
        sender = await event.get_sender()
        username = getattr(sender, "username", None) or str(sender.id)

        # Кэшируем input entity чтобы отвечать без access hash
        try:
            entity = await client.get_input_entity(sender)
            _entity_cache.setdefault(phone, {})[sender.id] = entity
        except Exception:
            pass

        async def _safe_call():
            try:
                await _on_incoming(phone, username, sender.id, event.raw_text)
            except Exception as e:
                print(f"[TG handler ERROR] {e}")
        asyncio.create_task(_safe_call())


async def load_active_accounts():
    """Загружает все активные аккаунты из БД и запускает клиентов."""
    conn = await _get_db()
    rows = await conn.fetch(
        "SELECT id, phone, session_str, session_str_enc FROM tg_accounts WHERE status='active'"
    )
    await conn.close()
    for row in rows:
        sess = None
        if row["session_str_enc"]:
            sess = _decrypt_session(row["session_str_enc"])
        if not sess and row["session_str"]:
            sess = row["session_str"]  # legacy plain-text
        if sess and row["phone"] not in _clients:
            await _start_client(row["phone"], sess, row["id"])


async def _start_client(phone: str, session_str: str, account_id: int):
    client = TelegramClient(
        StringSession(session_str),
        TG_API_ID,
        TG_API_HASH,
    )
    await client.connect()
    if not await client.is_user_authorized():
        print(f"[TG] Аккаунт {phone} — сессия устарела, нужна переавторизация")
        return

    _make_handler(phone, client)
    await client.start()
    _clients[phone] = client
    _account_ids[phone] = account_id
    _entity_cache.setdefault(phone, {})
    print(f"[TG] Клиент {phone} запущен и слушает входящие")


async def request_code(phone: str) -> dict:
    """Отправляет код подтверждения. Возвращает phone_code_hash и тип доставки."""
    client = TelegramClient(StringSession(), TG_API_ID, TG_API_HASH)
    await client.connect()
    result = await client.send_code_request(phone)
    code_type = type(result.type).__name__
    print(f"[TG] Код отправлен на {phone}. Тип доставки: {code_type}")
    _clients[f"_pending_{phone}"] = client
    return {"phone_code_hash": result.phone_code_hash, "code_type": code_type}


async def confirm_code(phone: str, code: str, phone_code_hash: str, tenant_id: int) -> str:
    """Подтверждает код, сохраняет сессию в БД, возвращает StringSession."""
    client = _clients.pop(f"_pending_{phone}", None)
    if client is None:
        raise ValueError("Сессия не найдена. Сначала запросите код.")
    await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    session_str = client.session.save()

    conn = await _get_db()
    # Если phone уже есть у другого тенанта — блокируем
    existing_owner = await conn.fetchval("SELECT tenant_id FROM tg_accounts WHERE phone=$1", phone)
    if existing_owner is not None and existing_owner != tenant_id:
        await conn.close()
        raise ValueError("Этот номер уже подключён к другому тенанту")
    enc = _encrypt_session(session_str)
    if enc:
        row = await conn.fetchrow(
            """INSERT INTO tg_accounts (phone, session_str_enc, status, tenant_id)
               VALUES ($1, $2, 'active', $3)
               ON CONFLICT (phone) DO UPDATE
                 SET session_str_enc=$2, session_str=NULL, status='active', tenant_id=$3
               RETURNING id""",
            phone, enc, tenant_id,
        )
    else:
        print("[tg-client] WARNING: BOT_TOKEN_ENC_KEY not set — session_str stored as plaintext")
        row = await conn.fetchrow(
            """INSERT INTO tg_accounts (phone, session_str, status, tenant_id)
               VALUES ($1, $2, 'active', $3)
               ON CONFLICT (phone) DO UPDATE
                 SET session_str=$2, status='active', tenant_id=$3
               RETURNING id""",
            phone, session_str, tenant_id,
        )
    await conn.close()

    _make_handler(phone, client)
    _clients[phone] = client
    _account_ids[phone] = row["id"]
    _entity_cache.setdefault(phone, {})
    print(f"[TG] Аккаунт {phone} авторизован и слушает входящие")
    return session_str


async def send_message(phone: str, user_id: int, text: str) -> int:
    """Отправляет сообщение пользователю по его TG user_id. Возвращает tg_msg_id."""
    client = _clients.get(phone)
    if client is None:
        raise RuntimeError(f"Клиент {phone} не запущен")

    # Сначала пробуем из кэша (есть access hash)
    entity = _entity_cache.get(phone, {}).get(user_id)
    if entity:
        msg = await client.send_message(entity, text)
        return msg.id

    # Если нет в кэше — пробуем получить напрямую
    try:
        entity = await client.get_input_entity(user_id)
        _entity_cache.setdefault(phone, {})[user_id] = entity
        msg = await client.send_message(entity, text)
        return msg.id
    except Exception as e:
        raise RuntimeError(f"Не удалось найти пользователя {user_id}: {e}")


async def send_message_by_username(phone: str, username: str, text: str) -> int:
    """Отправляет сообщение по username (для первого сообщения кампании)."""
    client = _clients.get(phone)
    if client is None:
        raise RuntimeError(f"Клиент {phone} не запущен")
    msg = await client.send_message(username, text)
    # Кэшируем entity после первой отправки
    try:
        entity = await client.get_input_entity(username)
        peer_id = getattr(entity, "user_id", None)
        if peer_id:
            _entity_cache.setdefault(phone, {})[peer_id] = entity
    except Exception:
        pass
    return msg.id


async def get_accounts_status(tenant_id: int) -> list[dict]:
    """Список аккаунтов конкретного тенанта с признаком онлайн."""
    conn = await _get_db()
    rows = await conn.fetch(
        "SELECT id, phone, status, daily_limit, sent_today FROM tg_accounts WHERE tenant_id=$1 ORDER BY id",
        tenant_id,
    )
    await conn.close()
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "phone": r["phone"],
            "status": r["status"],
            "daily_limit": r["daily_limit"],
            "sent_today": r["sent_today"],
            "online": r["phone"] in _clients,
        })
    return result
