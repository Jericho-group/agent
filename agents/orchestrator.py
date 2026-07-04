"""
Orchestrator — главный мозг системы на базе CrewAI.
"""

import asyncio
from functools import lru_cache

from crewai import Agent, Crew, LLM, Process, Task

from config import settings
from memory.dialogue_memory import DialogueMemory
from router.intent_router import IntentResult
from skills import (  # noqa: E402
    is_escalation,
    ESCALATION_RESPONSE,
    extract_bant,
)
from tools.crm_tool import CRMTool
from tools.rag_tool import RAGSearchTool

# ── Шаблоны системных промптов агентов ───────────────────────────────────────

_SALES_BACKSTORY = """Ты опытный специалист по продажам {company}.
Твоя задача — выяснить потребности клиента через точные вопросы (BANT: бюджет, полномочия, потребность, сроки),
представить ценность продукта, отработать возражения и направить к решению.
Используй апселл: если клиент готов купить, предложи более подходящий тариф или доп. функцию из базы знаний.
Ты не давишь, но уверенно ведёшь диалог. Всегда ищи информацию в базе знаний."""

_ADVISOR_BACKSTORY = """Ты эксперт по продукту {company}.
Ты глубоко знаешь все функции, тарифы и сценарии использования.
Твоя задача — выслушать требования клиента и подобрать оптимальное решение.
Если видишь возможность кросселла (сопутствующие продукты/тарифы) — предложи их с обоснованием.
Ты задаёшь уточняющие вопросы, а не даёшь обобщённые советы."""

_SUPPORT_BACKSTORY = """Ты специалист технической поддержки {company}.
Ты помогаешь клиентам разобраться с функциями приложения, решаешь проблемы
и объясняешь сложные вещи простыми словами. Всегда проверяй базу знаний."""

_GREETING_PROMPT = """Пользователь написал: "{message}"

Ответь дружелюбным приветствием от имени {company}.
Представься, спроси как можешь помочь. Коротко (2-3 предложения)."""

_OFF_TOPIC_PROMPT = """Пользователь написал: "{message}"

Вежливо объясни что ты чат-бот {company} и специализируешься
только на вопросах связанных с продуктом и услугами компании.
Предложи задать вопрос по теме. Коротко."""


def _make_llm(model: str) -> LLM:
    return LLM(
        model=model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )


@lru_cache(maxsize=1)
def _get_tools() -> tuple[RAGSearchTool, CRMTool]:
    return RAGSearchTool(), CRMTool()


def _build_agent(role: str, goal: str, backstory: str, tools: list, verbose: bool) -> Agent:
    return Agent(
        role=role,
        goal=goal,
        backstory=backstory.format(company=settings.company_name),
        tools=tools,
        llm=_make_llm(settings.agent_model),
        verbose=verbose,
        max_iter=6,
        allow_delegation=False,
    )


def _run_direct_llm(prompt: str) -> str:
    from openai import OpenAI
    client = OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    response = client.chat.completions.create(
        model=settings.router_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=300,
    )
    return response.choices[0].message.content or ""


import re as _re

_PARTNER_INTENT_RX = _re.compile(
    r"(мои\s+лид|сколько\s+у\s+меня\s+лид|сколько\s+я\s+привёл|"
    r"моя\s+статистика|мой\s+баланс|баланс\s+партн|статистика\s+по\s+партн|"
    r"сколько\s+мне\s+начислен|когда\s+выплат|к\s+выплате|выплат[аы]\s+партн|"
    r"что\s+у\s+меня\s+по\s+партн|история\s+начислен)",
    _re.IGNORECASE,
)
_TG_RX = _re.compile(r"@([A-Za-z0-9_]{3,32})")
_EMAIL_RX = _re.compile(r"\b([\w.+-]+@[\w-]+\.[\w.-]+)\b")


def _resolve_partner_contact(session_id: str | None, message: str) -> tuple[str, str] | None:
    """Возвращает (contact, source) либо None.
    Источники: 'tg-session' (из bot_404_tg_users), 'explicit' (из текста)."""
    from knowledge.vector_store import _get_conn

    em = _EMAIL_RX.search(message or "")
    if em:
        return (em.group(1).lower(), "explicit")
    tg = _TG_RX.search(message or "")
    if tg:
        return ("@" + tg.group(1).lower(), "explicit")

    if not session_id or not session_id.startswith("tg:"):
        return None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username FROM bot_404_tg_users WHERE session_id=%s AND username IS NOT NULL LIMIT 1",
                (session_id,),
            )
            row = cur.fetchone()
            u = row[0] if row else None
    except Exception as e:
        print(f"[partner-resolve] db failed: {e}")
        u = None
    if u:
        return ("@" + str(u).lower(), "tg-session")
    return None


def get_partner_stats(contact: str) -> dict | None:
    """Ищет партнёра по contact (case-insensitive), возвращает summary + leads.
    None если не найден."""
    from knowledge.vector_store import _get_conn
    import psycopg2.extras

    try:
        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT id, name, contact, rate_pct::float AS rate_pct, status,
                          joined_at, referral_code
                   FROM bot_404_partners WHERE lower(contact)=lower(%s) LIMIT 1""",
                (contact,),
            )
            partner = cur.fetchone()
            if not partner:
                return None
            cur.execute(
                """SELECT lead_name, status, reward_rub::float AS reward_rub,
                          payout_status, created_at
                   FROM bot_404_partner_leads WHERE partner_id=%s
                   ORDER BY created_at DESC LIMIT 50""",
                (partner["id"],),
            )
            leads = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[partner-stats] db failed: {e}")
        return None

    totals = {
        "leads_count": len(leads),
        "deals_count": sum(1 for l in leads if l["status"] == "deal"),
        "in_progress_count": sum(1 for l in leads if l["status"] == "in_progress"),
        "total_reward": sum(float(l["reward_rub"] or 0) for l in leads),
        "pending_reward": sum(float(l["reward_rub"] or 0) for l in leads if l["payout_status"] == "pending"),
        "paid_reward": sum(float(l["reward_rub"] or 0) for l in leads if l["payout_status"] == "paid"),
    }
    # joined_at → str для JSON-safe
    if partner.get("joined_at"):
        partner["joined_at"] = partner["joined_at"].isoformat()
    for l in leads:
        if l.get("created_at"):
            l["created_at"] = l["created_at"].isoformat()
    return {"partner": partner, "leads": leads, "totals": totals}


def _format_partner_block(stats: dict) -> str:
    """Форматирует stats как plain-text блок для system-prompt."""
    p = stats["partner"]
    t = stats["totals"]
    lines = [
        f"Партнёр найден: {p['name']} (контакт {p['contact']}, ставка {p['rate_pct']}%).",
        f"Лидов всего: {t['leads_count']}; из них сделок: {t['deals_count']}, в работе: {t['in_progress_count']}.",
        f"Начислено всего: {t['total_reward']:.2f} ₽ (выплачено {t['paid_reward']:.2f} ₽, к выплате {t['pending_reward']:.2f} ₽).",
    ]
    if stats["leads"]:
        lines.append("Список лидов (последние):")
        for l in stats["leads"][:10]:
            st = {"new": "новый", "in_progress": "в работе", "deal": "сделка", "rejected": "отказ"}.get(l["status"], l["status"])
            pay = "выплачено" if l["payout_status"] == "paid" else "ожидает"
            lines.append(f"  • {l['lead_name']} — {st}, {float(l['reward_rub'] or 0):.2f} ₽ ({pay})")
    return "\n".join(lines)


def _run_kb_grounded(message: str, history_text: str, session_id: str | None = None) -> str:
    """FAQ/support fast path: RAG → отдаём результат в prompt → прямой LLM-ответ.
    Обходит ненадёжный tool-calling CrewAI через aitunnel.
    Доп. слой: для партнёрских вопросов подмешивает live-статистику из БД."""
    from openai import OpenAI
    from knowledge.vector_store import VectorStore

    # ── Партнёрский intent: пытаемся идентифицировать и подложить статистику ──
    partner_block = ""
    if _PARTNER_INTENT_RX.search(message or ""):
        # ищем контакт сначала в текущем сообщении, затем в истории диалога
        contact = _resolve_partner_contact(session_id, message)
        if not contact and history_text:
            contact = _resolve_partner_contact(session_id, history_text)
        if contact:
            stats = get_partner_stats(contact[0])
            if stats:
                partner_block = _format_partner_block(stats)
            else:
                partner_block = (
                    f"Контакт {contact[0]} (источник: {contact[1]}) — в базе партнёров не найден. "
                    "Скажи клиенту, что под этим контактом партнёра нет, и попроси уточнить email "
                    "или @username, под которым он зарегистрирован."
                )
        else:
            partner_block = (
                "Партнёрский intent распознан, но контакт не определён "
                "(нет TG-сессии и в сообщении нет @username/email). "
                "Попроси клиента указать email или @telegram, под которым он зарегистрирован партнёром."
            )

    store = VectorStore()
    kb_block = ""
    try:
        results = store.search(query=message, n_results=4) or []
        relevant = [r for r in results if r["distance"] < 0.65]
        if relevant:
            chunks = []
            for r in relevant:
                title = r["metadata"].get("title") or ""
                cat = r["metadata"].get("category") or ""
                head = f"[{title} · {cat}]" if title else ""
                chunks.append(f"{head}\n{r['document']}")
            kb_block = "\n\n---\n\n".join(chunks)
    except Exception as e:
        print(f"[kb] search failed: {e}")

    system = (
        f"Ты — ассистент {settings.company_name}. Отвечаешь на вопросы клиента "
        f"строго на основе блоков 'База знаний' и 'Данные партнёра' ниже. Если в блоках есть прямой ответ — "
        f"используй его, цитируй конкретные цифры и условия из них. Если ответа нет — "
        f"честно скажи и предложи связаться с поддержкой. "
        f"Когда есть блок 'Данные партнёра' — представь статистику ясно и по делу (имя, число лидов, "
        f"сумма к выплате, список последних лидов), не выдумывай цифры. "
        f"Отвечай дружелюбно, кратко (3-6 предложений), на языке клиента."
    )
    user = f"""=== История диалога ===
{history_text or '(пусто)'}

=== База знаний ===
{kb_block or '(релевантных статей не найдено)'}

=== Данные партнёра ===
{partner_block or '(вопрос не партнёрский или контакт не определён)'}

=== Новое сообщение клиента ===
{message}"""

    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    response = client.chat.completions.create(
        model=settings.router_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.4,
        max_tokens=500,
    )
    return response.choices[0].message.content or ""


def _run_crew(agent: Agent, task_description: str) -> str:
    task = Task(
        description=task_description,
        expected_output=(
            "Профессиональный, дружелюбный ответ клиенту. "
            "Без технических артефактов. Только текст ответа."
        ),
        agent=agent,
    )
    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=settings.debug,
    )
    result = crew.kickoff()
    return str(result).strip()


class Orchestrator:
    """Основной оркестратор: принимает сообщение + интент, возвращает ответ."""

    def __init__(self) -> None:
        self.memory = DialogueMemory()

    async def process(
        self,
        message: str,
        session_id: str,
        intent_result: IntentResult,
    ) -> str:
        intent = intent_result.intent
        history = await self.memory.get_history(session_id)
        history_text = self.memory.format_for_agent(history)

        # ── Эскалация (перехват перед агентами) ──────────────────────────────
        if is_escalation(message):
            await self.memory.upsert_session_meta(session_id, escalated=True)
            response = ESCALATION_RESPONSE
            intent = "escalation_request"

        # ── Быстрые пути (без CrewAI) ────────────────────────────────────────
        elif intent == "greeting":
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                _run_direct_llm,
                _GREETING_PROMPT.format(message=message, company=settings.company_name),
            )

        elif intent == "off_topic":
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                _run_direct_llm,
                _OFF_TOPIC_PROMPT.format(message=message, company=settings.company_name),
            )

        # ── CrewAI агенты ─────────────────────────────────────────────────────
        elif intent in ("sales_inquiry", "qualification"):
            response = await self._run_sales_agent(message, session_id, history_text)

        elif intent == "product_advice":
            response = await self._run_advisor_agent(message, history_text)

        else:  # simple_faq, app_support — прямой KB-grounded путь (надёжнее tool-calling)
            response = await asyncio.get_event_loop().run_in_executor(
                None, _run_kb_grounded, message, history_text, session_id
            )

        # ── Сохраняем в память (с intent для аналитики) ──────────────────────
        await self.memory.add_message(session_id, "user", message, intent=intent)
        await self.memory.add_message(session_id, "assistant", response, intent=intent)

        # ── Пост-обработка: BANT для sales / qualification ───────────────────
        if intent in ("sales_inquiry", "qualification"):
            asyncio.create_task(self._update_bant(session_id))

        return response

    async def _update_bant(self, session_id: str) -> None:
        """В фоне: обновить BANT по последней истории. Ошибки глушим."""
        try:
            history = await self.memory.get_history(session_id, limit=12)
            history_text = self.memory.format_for_agent(history)
            bant = await asyncio.get_event_loop().run_in_executor(
                None, extract_bant, history_text
            )
            if "error" not in bant:
                await self.memory.upsert_session_meta(session_id, bant=bant)
        except Exception as e:
            print(f"[bant] extract failed for {session_id}: {e}")

    # ── Агенты ───────────────────────────────────────────────────────────────

    async def _run_sales_agent(
        self, message: str, session_id: str, history_text: str
    ) -> str:
        rag_tool, crm_tool = _get_tools()
        agent = _build_agent(
            role="Sales Specialist",
            goal=(
                "Выяснить BANT (бюджет, полномочия, потребность, сроки), "
                "квалифицировать лид и направить к покупке. При явном сигнале — предложить апселл."
            ),
            backstory=_SALES_BACKSTORY,
            tools=[rag_tool, crm_tool],
            verbose=settings.debug,
        )
        task_description = f"""
История диалога:
{history_text}

Новое сообщение клиента: {message}
ID сессии клиента: {session_id}

Задача:
1. При необходимости — загляни в CRM (используй session_id как client_id).
2. Ищи релевантную информацию в базе знаний (цены, функции, сравнения).
3. Задай 1-2 уточняющих вопроса (BANT) если нужно выяснить бюджет/роль/срок.
4. Представь ценность продукта конкретно под ситуацию клиента.
5. Если клиент готов — ненавязчиво предложи апселл (более подходящий тариф или доп. функцию).
6. Предложи следующий шаг: демо, пробный период, консультацию.
Отвечай на языке клиента. Будь профессионален и дружелюбен."""
        return await asyncio.get_event_loop().run_in_executor(
            None, _run_crew, agent, task_description
        )

    async def _run_advisor_agent(self, message: str, history_text: str) -> str:
        rag_tool, _ = _get_tools()
        agent = _build_agent(
            role="Product Advisor",
            goal=(
                "Подобрать оптимальное решение под требования клиента, "
                "опираясь на базу знаний. По возможности предложить кросселл."
            ),
            backstory=_ADVISOR_BACKSTORY,
            tools=[rag_tool],
            verbose=settings.debug,
        )
        task_description = f"""
История диалога:
{history_text}

Новое сообщение клиента: {message}

Задача:
1. Ищи в базе знаний информацию о функциях, тарифах, кейсах.
2. Задай уточняющий вопрос если не хватает данных о потребностях.
3. Дай конкретную персонализированную рекомендацию с обоснованием.
4. Если уместно — предложи сопутствующий продукт/тариф (кросселл) с обоснованием.
5. Если есть несколько вариантов — сравни их по ключевым критериям клиента.
Отвечай на языке клиента."""
        return await asyncio.get_event_loop().run_in_executor(
            None, _run_crew, agent, task_description
        )

    async def _run_support_agent(self, message: str, history_text: str) -> str:
        rag_tool, _ = _get_tools()
        agent = _build_agent(
            role="Support Specialist",
            goal=(
                "Помочь клиенту разобраться с вопросом, "
                "дать точную информацию из базы знаний."
            ),
            backstory=_SUPPORT_BACKSTORY,
            tools=[rag_tool],
            verbose=settings.debug,
        )
        task_description = f"""
История диалога:
{history_text}

Новое сообщение клиента: {message}

Задача:
1. Ищи в базе знаний точный ответ на вопрос клиента.
2. Если это how-to вопрос — дай пошаговую инструкцию.
3. Если информации нет — честно скажи и предложи связаться с поддержкой.
4. Объясняй просто, без технического жаргона.
Отвечай на языке клиента."""
        return await asyncio.get_event_loop().run_in_executor(
            None, _run_crew, agent, task_description
        )
