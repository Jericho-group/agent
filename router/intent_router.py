"""
Intent Router — лёгкий классификатор запросов.

Двухэтапный подход:
  1. Keyword matching (мгновенно, 0 токенов)
  2. LLM fallback (только если keywords не дали уверенности)

Интенты:
  greeting       — приветствие
  simple_faq     — простой фактический вопрос
  sales_inquiry  — цена, покупка, демо
  product_advice — рекомендации, сравнения
  app_support    — как пользоваться, ошибки
  qualification  — клиент хочет понять, подходит ли продукт
  off_topic      — не по теме
"""

import json
import re
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel

from config import settings

# ── Типы интентов ────────────────────────────────────────────────────────────

IntentType = Literal[
    "greeting",
    "simple_faq",
    "sales_inquiry",
    "product_advice",
    "app_support",
    "qualification",
    "off_topic",
]

HEAVY_INTENTS = {"sales_inquiry", "product_advice", "qualification"}
LIGHT_INTENTS = {"greeting", "simple_faq", "app_support", "off_topic"}


class IntentResult(BaseModel):
    intent: IntentType
    confidence: float        # 0.0 – 1.0
    used_llm: bool = False   # для метрик: тратили ли токены


# ── Keyword rules ─────────────────────────────────────────────────────────────

_KEYWORD_RULES: list[tuple[IntentType, list[str], float]] = [
    # (intent, keywords, score_per_hit)
    ("greeting", [
        r"\bhi\b", r"\bhello\b", r"\bhey\b",
        r"привет", r"добрый\s*(день|утро|вечер)", r"здравствуй",
        r"хай", r"хэй",
    ], 0.9),
    ("sales_inquiry", [
        r"цен[аыу]", r"сколько\s*стоит", r"прайс", r"тариф",
        r"купить", r"приобрести", r"оплат", r"счёт", r"инвойс",
        r"\bprice\b", r"\bcost\b", r"\bbuy\b", r"\bpurchase\b",
        r"демо", r"demo", r"trial", r"пробн",
        r"скидк", r"акци[яи]", r"промокод",
    ], 0.85),
    ("product_advice", [
        r"рекоменд", r"посоветуй", r"что лучше", r"какой выбрать",
        r"сравни", r"подойдёт ли", r"подберите",
        r"\brecommend\b", r"\bbest\b", r"\bcompare\b", r"\bsuggest\b",
        r"для меня", r"под мои задачи",
    ], 0.85),
    ("app_support", [
        r"как\s+(настроить|установить|подключить|использовать|включить|отключить)",
        r"не работает", r"ошибка", r"баг", r"проблем",
        r"\berror\b", r"\bbug\b", r"\bissue\b", r"\bnot working\b",
        r"how to", r"how do i",
        r"инструкци", r"руководств",
    ], 0.85),
    ("qualification", [
        r"подойдёт ли", r"могу ли я", r"подходит ли",
        r"есть ли у вас", r"поддерживаете ли",
        r"does it support", r"can i use", r"is it possible",
        r"интеграци", r"integration",
    ], 0.80),
    ("simple_faq", [
        r"что такое", r"что это", r"расскажи(те)?\s*о",
        r"\bwhat is\b", r"\btell me about\b", r"\bwhat are\b",
        r"функции", r"features", r"возможности",
    ], 0.75),
]

_SYSTEM_PROMPT = """You are an intent classifier for a B2B SaaS sales chatbot.

Classify the user message into exactly one intent:
- greeting      : hello, hi, general pleasantries
- simple_faq    : factual question about product/company that needs a direct answer
- sales_inquiry : pricing, buying, invoices, demo requests, discounts
- product_advice: asking for recommendations, comparisons, what suits their case
- app_support   : how-to questions, errors, bugs, configuration help
- qualification : user wants to know if the product fits their specific situation/requirements
- off_topic     : completely unrelated to the product or company

Reply ONLY with valid JSON:
{"intent": "<one of the above>", "confidence": <0.0-1.0>, "reasoning": "<brief>"}"""


def _keyword_score(text: str) -> dict[IntentType, float]:
    """Возвращает словарь intent → max_score по keyword matching."""
    text_lower = text.lower()
    scores: dict[IntentType, float] = {}
    for intent, patterns, score in _KEYWORD_RULES:
        for pattern in patterns:
            if re.search(pattern, text_lower):
                if intent not in scores or scores[intent] < score:
                    scores[intent] = score
    return scores


def classify_intent(message: str, history: list[dict] | None = None) -> IntentResult:
    """
    Классифицирует интент входящего сообщения.
    history — список dict {"role": "user"/"assistant", "content": "..."}
    """
    # ── Шаг 1: keyword matching ──────────────────────────────────────────────
    scores = _keyword_score(message)

    if scores:
        best_intent = max(scores, key=lambda k: scores[k])
        best_score = scores[best_intent]

        if best_score >= settings.keyword_confidence_threshold:
            return IntentResult(
                intent=best_intent,
                confidence=best_score,
                used_llm=False,
            )

    # ── Шаг 2: LLM fallback ──────────────────────────────────────────────────
    client = OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]

    # Добавляем последние 4 сообщения контекста (без system)
    if history:
        for msg in history[-4:]:
            messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": message})

    response = client.chat.completions.create(
        model=settings.router_model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=150,
    )

    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)

    return IntentResult(
        intent=data.get("intent", "simple_faq"),
        confidence=float(data.get("confidence", 0.7)),
        used_llm=True,
    )


def needs_heavy_agent(intent: IntentType) -> bool:
    """True если интент требует полноценного CrewAI агента."""
    return intent in HEAVY_INTENTS
