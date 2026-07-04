"""
Менеджер кампаний и воркер рассылки.
Отвечает за:
- CRUD кампаний и контактов
- Постановку в очередь с рандомными задержками
- Отправку первых сообщений
- Обработку входящих ответов через оркестратор
"""
from __future__ import annotations

import asyncio
import random
import uuid
from datetime import date, datetime

import asyncpg

from agents.orchestrator import Orchestrator
from config import settings
from memory.dialogue_memory import DialogueMemory
from proactive import tg_client

_orchestrator = Orchestrator()
_memory = DialogueMemory()

# contact_id → session_id оркестратора (in-memory кэш)
_contact_sessions: dict[int, str] = {}


async def _get_db() -> asyncpg.Connection:
    return await asyncpg.connect(settings.supabase_db_url)


# ── CRUD кампаний ─────────────────────────────────────────────────────────────

async def list_campaigns(tenant_id: int) -> list[dict]:
    conn = await _get_db()
    rows = await conn.fetch("""
        SELECT c.*, a.phone AS account_phone,
               COUNT(cc.id) AS total_contacts,
               SUM(CASE WHEN cc.status='sent' THEN 1 ELSE 0 END) AS sent,
               SUM(CASE WHEN cc.status='replied' THEN 1 ELSE 0 END) AS replied
        FROM campaigns c
        LEFT JOIN tg_accounts a ON a.id = c.account_id
        LEFT JOIN campaign_contacts cc ON cc.campaign_id = c.id
        WHERE c.tenant_id = $1
        GROUP BY c.id, a.phone
        ORDER BY c.created_at DESC
    """, tenant_id)
    await conn.close()
    return [dict(r) for r in rows]


async def get_campaign(campaign_id: int, tenant_id: int) -> dict | None:
    conn = await _get_db()
    row = await conn.fetchrow(
        "SELECT * FROM campaigns WHERE id=$1 AND tenant_id=$2",
        campaign_id, tenant_id
    )
    await conn.close()
    return dict(row) if row else None


async def create_campaign(data: dict, tenant_id: int) -> dict:
    conn = await _get_db()
    # Проверяем, что аккаунт (если указан) принадлежит этому тенанту
    if data.get("account_id"):
        owner = await conn.fetchval(
            "SELECT tenant_id FROM tg_accounts WHERE id=$1", data["account_id"]
        )
        if owner != tenant_id:
            await conn.close()
            raise ValueError("account_id принадлежит другому тенанту")
    row = await conn.fetchrow("""
        INSERT INTO campaigns (name, account_id, first_message, goal, delay_min, delay_max, tenant_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *
    """, data["name"], data.get("account_id"), data["first_message"],
        data.get("goal"), data.get("delay_min", 60), data.get("delay_max", 180),
        tenant_id)
    await conn.close()
    return dict(row)


async def update_campaign_status(campaign_id: int, status: str, tenant_id: int):
    conn = await _get_db()
    await conn.execute(
        "UPDATE campaigns SET status=$1 WHERE id=$2 AND tenant_id=$3",
        status, campaign_id, tenant_id
    )
    await conn.close()


async def delete_campaign(campaign_id: int, tenant_id: int):
    conn = await _get_db()
    await conn.execute(
        "DELETE FROM campaigns WHERE id=$1 AND tenant_id=$2",
        campaign_id, tenant_id
    )
    await conn.close()


# ── Контакты ──────────────────────────────────────────────────────────────────

async def add_contacts(campaign_id: int, contacts: list[dict], tenant_id: int) -> int:
    conn = await _get_db()
    # Проверяем что кампания принадлежит тенанту
    owner = await conn.fetchval(
        "SELECT tenant_id FROM campaigns WHERE id=$1", campaign_id
    )
    if owner != tenant_id:
        await conn.close()
        raise ValueError("Кампания не найдена")
    added = 0
    for c in contacts:
        await conn.execute("""
            INSERT INTO campaign_contacts (campaign_id, username, phone, name, tenant_id)
            VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING
        """, campaign_id, c.get("username"), c.get("phone"), c.get("name"), tenant_id)
        added += 1
    await conn.close()
    return added


async def list_contacts(campaign_id: int, tenant_id: int) -> list[dict]:
    conn = await _get_db()
    rows = await conn.fetch(
        "SELECT * FROM campaign_contacts WHERE campaign_id=$1 AND tenant_id=$2 ORDER BY created_at DESC",
        campaign_id, tenant_id
    )
    await conn.close()
    return [dict(r) for r in rows]


# ── Воркер рассылки ───────────────────────────────────────────────────────────

async def start_campaign(campaign_id: int, tenant_id: int, reset: bool = False):
    """Ставит контактов в очередь и запускает воркер.
    reset=True — сбросить статусы всех контактов и отправить заново.
    """
    conn = await _get_db()
    campaign = await conn.fetchrow(
        "SELECT * FROM campaigns WHERE id=$1 AND tenant_id=$2",
        campaign_id, tenant_id
    )
    if not campaign:
        await conn.close()
        raise ValueError("Кампания не найдена")

    if reset:
        # Сбрасываем статусы контактов и очередь
        await conn.execute(
            "UPDATE campaign_contacts SET status='pending', sent_at=NULL, session_id=NULL WHERE campaign_id=$1",
            campaign_id
        )
        await conn.execute(
            "DELETE FROM outreach_queue WHERE campaign_id=$1", campaign_id
        )

    contacts = await conn.fetch(
        "SELECT * FROM campaign_contacts WHERE campaign_id=$1 AND status='pending'",
        campaign_id
    )
    await conn.execute(
        "UPDATE campaigns SET status='active' WHERE id=$1", campaign_id
    )
    await conn.close()

    delay_acc = 0
    for contact in contacts:
        conn2 = await _get_db()
        await conn2.execute("""
            INSERT INTO outreach_queue (contact_id, campaign_id, scheduled_at)
            VALUES ($1, $2, NOW() + $3 * INTERVAL '1 second')
        """, contact["id"], campaign_id, delay_acc)
        await conn2.close()
        delay_acc += random.randint(campaign["delay_min"], campaign["delay_max"])

    asyncio.create_task(_run_queue(campaign_id))


async def _run_queue(campaign_id: int):
    """Воркер: берёт задания из очереди и отправляет в нужное время."""
    conn = await _get_db()
    campaign = await conn.fetchrow("SELECT * FROM campaigns WHERE id=$1", campaign_id)
    account = await conn.fetchrow(
        "SELECT * FROM tg_accounts WHERE id=$1", campaign["account_id"]
    )
    await conn.close()

    phone = account["phone"]

    while True:
        conn = await _get_db()

        # Проверяем что кампания всё ещё активна
        status = await conn.fetchval(
            "SELECT status FROM campaigns WHERE id=$1", campaign_id
        )
        if status != "active":
            await conn.close()
            break

        # Берём следующее задание
        job = await conn.fetchrow("""
            SELECT q.*, cc.username, cc.phone AS contact_phone, cc.name
            FROM outreach_queue q
            JOIN campaign_contacts cc ON cc.id = q.contact_id
            WHERE q.campaign_id=$1
              AND q.status='pending'
              AND q.scheduled_at <= NOW()
            ORDER BY q.scheduled_at
            LIMIT 1
        """, campaign_id)

        if not job:
            # Ждём следующего задания
            next_job = await conn.fetchrow("""
                SELECT scheduled_at FROM outreach_queue
                WHERE campaign_id=$1 AND status='pending'
                ORDER BY scheduled_at LIMIT 1
            """, campaign_id)
            await conn.close()
            if not next_job:
                # Все отправлены
                conn2 = await _get_db()
                await conn2.execute(
                    "UPDATE campaigns SET status='done' WHERE id=$1", campaign_id
                )
                await conn2.close()
                break
            wait_secs = max(1, (next_job["scheduled_at"] - datetime.now().astimezone()).total_seconds())
            await asyncio.sleep(min(wait_secs, 30))
            continue

        # Сброс дневного счётчика если нужно
        if account["last_reset"] < date.today():
            await conn.execute(
                "UPDATE tg_accounts SET sent_today=0, last_reset=CURRENT_DATE WHERE id=$1",
                account["id"]
            )
            account = dict(account)
            account["sent_today"] = 0

        # Проверяем лимит
        if account["sent_today"] >= account["daily_limit"]:
            await conn.close()
            await asyncio.sleep(60)
            continue

        recipient = job["username"] or job["contact_phone"]
        try:
            msg_id = await tg_client.send_message_by_username(phone, recipient, campaign["first_message"])

            session_id = str(uuid.uuid4())
            await conn.execute("""
                UPDATE campaign_contacts
                SET status='sent', sent_at=NOW(), session_id=$1
                WHERE id=$2
            """, session_id, job["contact_id"])
            await conn.execute(
                "UPDATE outreach_queue SET status='sent' WHERE id=$1", job["id"]
            )
            await conn.execute(
                "UPDATE tg_accounts SET sent_today=sent_today+1 WHERE id=$1", account["id"]
            )
            await conn.execute("""
                INSERT INTO outreach_messages (contact_id, direction, content, tg_msg_id)
                VALUES ($1,'out',$2,$3)
            """, job["contact_id"], campaign["first_message"], msg_id)

            _contact_sessions[job["contact_id"]] = session_id

        except Exception as e:
            await conn.execute(
                "UPDATE outreach_queue SET status='failed', error=$1 WHERE id=$2",
                str(e), job["id"]
            )
            await conn.execute(
                "UPDATE campaign_contacts SET status='failed' WHERE id=$1",
                job["contact_id"]
            )

        await conn.close()
        await asyncio.sleep(1)


# ── Обработка входящих ответов ────────────────────────────────────────────────

async def handle_incoming(phone: str, username: str, tg_user_id: int, text: str):
    """Вызывается при входящем сообщении. Передаёт в оркестратор и отвечает."""
    print(f"[TG incoming] from=@{username} (id={tg_user_id}) text={text!r}")
    conn = await _get_db()

    # Ищем контакт по username (с @ и без) или по tg_user_id
    clean = username.lstrip("@")
    contact = await conn.fetchrow("""
        SELECT cc.*, c.goal, c.account_id,
               a.phone AS account_phone
        FROM campaign_contacts cc
        JOIN campaigns c ON c.id = cc.campaign_id
        JOIN tg_accounts a ON a.id = c.account_id
        WHERE (
            cc.username = $1 OR cc.username = $2 OR cc.username = $3
            OR cc.tg_user_id = $4
        )
        ORDER BY cc.sent_at DESC NULLS LAST LIMIT 1
    """, clean, f"@{clean}", str(tg_user_id), tg_user_id)
    await conn.close()

    if not contact:
        # Контакт не найден — сохраняем на будущее и отвечаем базово
        print(f"[TG incoming] контакт @{username} не найден в базе, пропускаем")
        return

    contact_id = contact["id"]
    session_id = contact["session_id"] or _contact_sessions.get(contact_id) or str(uuid.uuid4())

    # Сохраняем входящее сообщение
    conn = await _get_db()
    await conn.execute("""
        INSERT INTO outreach_messages (contact_id, direction, content)
        VALUES ($1,'in',$2)
    """, contact_id, text)
    await conn.execute(
        "UPDATE campaign_contacts SET status='replied' WHERE id=$1", contact_id
    )
    await conn.close()

    # Запускаем оркестратор
    try:
        from router.intent_router import classify_intent
        history = await _memory.get_history(session_id, limit=6)
        intent_result = classify_intent(text, history)
        response = await _orchestrator.process(
            message=text,
            session_id=session_id,
            intent_result=intent_result,
        )
        print(f"[TG outgoing] → @{username}: {response!r}")
    except Exception as e:
        print(f"[TG orchestrator ERROR] {e}")
        response = "Спасибо за ответ, скоро свяжемся с вами!"

    # Отправляем ответ — используем int user_id (с кэшем entity)
    await tg_client.send_message(phone, tg_user_id, response)

    # Сохраняем исходящий ответ
    conn = await _get_db()
    await conn.execute("""
        INSERT INTO outreach_messages (contact_id, direction, content)
        VALUES ($1,'out',$2)
    """, contact_id, response)
    await conn.close()


# ── Диалоги для просмотра в админке ──────────────────────────────────────────

async def list_conversations(tenant_id: int, campaign_id: int | None = None) -> list[dict]:
    conn = await _get_db()
    if campaign_id:
        where = "WHERE cc.tenant_id=$1 AND cc.campaign_id=$2"
        params = [tenant_id, campaign_id]
    else:
        where = "WHERE cc.tenant_id=$1"
        params = [tenant_id]
    rows = await conn.fetch(f"""
        SELECT cc.id, cc.username, cc.name, cc.status, cc.campaign_id,
               c.name AS campaign_name,
               COUNT(om.id) AS msg_count,
               MAX(om.created_at) AS last_message_at
        FROM campaign_contacts cc
        JOIN campaigns c ON c.id = cc.campaign_id
        LEFT JOIN outreach_messages om ON om.contact_id = cc.id
        {where}
        GROUP BY cc.id, c.name
        ORDER BY last_message_at DESC NULLS LAST
    """, *params)
    await conn.close()
    return [dict(r) for r in rows]


async def get_conversation_messages(contact_id: int, tenant_id: int) -> list[dict]:
    conn = await _get_db()
    # Проверяем что контакт принадлежит тенанту
    owner = await conn.fetchval(
        "SELECT tenant_id FROM campaign_contacts WHERE id=$1", contact_id
    )
    if owner != tenant_id:
        await conn.close()
        return []
    rows = await conn.fetch("""
        SELECT direction, content, created_at
        FROM outreach_messages WHERE contact_id=$1
        ORDER BY created_at
    """, contact_id)
    await conn.close()
    return [dict(r) for r in rows]
