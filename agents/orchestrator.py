"""
Orchestrator — главный мозг системы на базе CrewAI.

Логика маршрутизации:
  greeting   → прямой быстрый ответ (без CrewAI)
  off_topic  → прямой вежливый отказ (без CrewAI)
  simple_faq → лёгкий Support агент
  app_support→ Support агент
  sales_inquiry / qualification → Sales агент
  product_advice → Advisor агент

Все агенты используют RAGSearchTool для поиска по базе знаний.
Sales агент дополнительно имеет CRMTool.
"""

import asyncio
from functools import lru_cache

from crewai import Agent, Crew, LLM, Process, Task

from config import settings
from memory.dialogue_memory import DialogueMemory
from router.intent_router import IntentResult
from tools.crm_tool import CRMTool
from tools.rag_tool import RAGSearchTool

# ── Шаблоны системных промптов агентов ───────────────────────────────────────

_SALES_BACKSTORY = """Ты опытный специалист по продажам {company}.
Твоя задача — выяснить потребности клиента через точные вопросы,
представить ценность продукта, отработать возражения и направить к решению.
Ты не давишь, но уверенно ведёшь диалог. Всегда ищи информацию в базе знаний."""

_ADVISOR_BACKSTORY = """Ты эксперт по продукту {company}.
Ты глубоко знаешь все функции, тарифы и сценарии использования.
Твоя задача — выслушать требования клиента и подобрать оптимальное решение.
Ты задаёшь уточняющие вопросы, а не даёшь обобщённые советы."""

_SUPPORT_BACKSTORY = """Ты специалист технической поддержки {company}.
Ты помогаешь клиентам разобраться с функциями приложения, решаешь проблемы
и объясняешь сложные вещи простыми словами. Всегда проверяй базу знаний."""

# ── Прямые ответы без агентов ────────────────────────────────────────────────

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
    """Инициализируем инструменты один раз."""
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
    """Быстрый LLM-вызов без CrewAI для приветствий и off-topic."""
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
        """
        Главный метод обработки сообщения.
        Выбирает стратегию (прямой LLM / CrewAI агент) по интенту.
        """
        intent = intent_result.intent
        history = await self.memory.get_history(session_id)
        history_text = self.memory.format_for_agent(history)

        # ── Быстрые пути (без CrewAI) ────────────────────────────────────────

        if intent == "greeting":
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                _run_direct_llm,
                _GREETING_PROMPT.format(
                    message=message, company=settings.company_name
                ),
            )

        elif intent == "off_topic":
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                _run_direct_llm,
                _OFF_TOPIC_PROMPT.format(
                    message=message, company=settings.company_name
                ),
            )

        # ── CrewAI агенты ─────────────────────────────────────────────────────

        elif intent in ("sales_inquiry", "qualification"):
            response = await self._run_sales_agent(message, session_id, history_text)

        elif intent == "product_advice":
            response = await self._run_advisor_agent(message, history_text)

        else:  # simple_faq, app_support
            response = await self._run_support_agent(message, history_text)

        # ── Сохраняем в память ───────────────────────────────────────────────
        await self.memory.add_message(session_id, "user", message)
        await self.memory.add_message(session_id, "assistant", response)

        return response

    # ── Агенты ───────────────────────────────────────────────────────────────

    async def _run_sales_agent(
        self, message: str, session_id: str, history_text: str
    ) -> str:
        rag_tool, crm_tool = _get_tools()
        agent = _build_agent(
            role="Sales Specialist",
            goal=(
                "Выяснить потребности клиента, квалифицировать лид "
                "и направить к покупке или следующему шагу (демо, консультация)."
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
3. Задай 1-2 уточняющих вопроса если нужно выяснить потребности.
4. Представь ценность продукта конкретно под ситуацию клиента.
5. Предложи следующий шаг: демо, пробный период, консультацию.
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
                "опираясь на базу знаний."
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
4. Если есть несколько вариантов — сравни их по ключевым критериям клиента.
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
