"""
Skills module — расширенные навыки оркестратора (BANT, эскалация, аналитика).

Подключается из agents/orchestrator.py после получения ответа агента.
Все LLM-вызовы — gpt-4o-mini, ~50–200 токенов. Не нагружает сервер.
"""

from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from config import settings


# ── Каталог навыков (для UI /admin/api/skills) ───────────────────────────────

SKILLS_CATALOG: list[dict] = [
    # category — для группировки; status: active | beta | coming_soon
    {"id": "bant",           "group": "Лидогенерация",  "title": "BANT-квалификация",       "icon": "🎯", "status": "active",
     "desc": "Извлекает бюджет, срок, потребность, роль покупателя. Горячие лиды подсвечиваются."},
    {"id": "upsell",         "group": "Монетизация",    "title": "Апселл и кросселл",        "icon": "💎", "status": "active",
     "desc": "Sales/Advisor агенты предлагают дополнительные тарифы и продукты по контексту."},
    {"id": "escalation",     "group": "Контроль",       "title": "Эскалация на оператора",  "icon": "🚨", "status": "active",
     "desc": "Ловит сигналы 'хочу менеджера' — помечает сессию флагом и отдаёт краткий ответ."},
    {"id": "faq",            "group": "FAQ",            "title": "База знаний и FAQ",        "icon": "📚", "status": "active",
     "desc": "Support-агент отвечает на типовые вопросы с поиском по pgvector."},
    {"id": "product_advice", "group": "Продажи",        "title": "Презентация продукта",     "icon": "🎤", "status": "active",
     "desc": "Advisor сравнивает тарифы, подбирает решение и работает с возражениями."},
    {"id": "reactivation",   "group": "Реактивация",    "title": "Возврат ушедших клиентов", "icon": "🔁", "status": "active",
     "desc": "Proactive-модуль шлёт персональные сообщения неактивным контактам из кампаний."},
    {"id": "partner_program","group": "Партнёрка",      "title": "Партнёрская программа",    "icon": "🤝", "status": "active",
     "desc": "Бот рассказывает условия партнёрки (10% с рекуррентных платежей), процесс подключения и обещает показать статистику лидов после идентификации."},
    {"id": "analytics",      "group": "Анализ",         "title": "Конверсионная аналитика", "icon": "📈", "status": "active",
     "desc": "Счётчики по интентам, эскалациям, BANT-квалификации. Виден на Дашборде."},
    {"id": "dialog_review",  "group": "Анализ",         "title": "Анализ диалогов",          "icon": "🔍", "status": "active",
     "desc": "Топ-интенты и количество сообщений на сессию из таблицы messages."},
    {"id": "nps",            "group": "NPS",            "title": "Сбор отзывов и оценок",   "icon": "⭐", "status": "beta",
     "desc": "Хранилище nps_score в session_meta. Триггер-отправка — когда будет планировщик."},
    {"id": "drip",           "group": "Воронка",        "title": "Прогрев цепочкой",         "icon": "🌡️", "status": "coming_soon",
     "desc": "Последовательность сообщений по дням. Нужен фоновой планировщик (APScheduler)."},
    {"id": "booking",        "group": "Запись",         "title": "Запись на встречу",        "icon": "📅", "status": "coming_soon",
     "desc": "Слоты и подтверждения. Требует таблицу слотов + интеграцию календаря."},
    {"id": "reminders",      "group": "Напоминания",    "title": "Авто-напоминания",         "icon": "🔔", "status": "coming_soon",
     "desc": "За 24ч и 1ч до встречи. Зависит от модуля booking."},
    {"id": "ab_test",        "group": "Рассылки",       "title": "A/B-тесты заголовков",    "icon": "🧪", "status": "coming_soon",
     "desc": "Случайное деление контактов на варианты. Нужна доработка campaigns-схемы."},
    {"id": "roi",            "group": "ROI",            "title": "Дашборд эффективности",    "icon": "💰", "status": "coming_soon",
     "desc": "ROI / стоимость диалога. Требует данные об оплатах и стоимости LLM-токенов."},
]


# ── Эскалация: ключевые слова + детектор ─────────────────────────────────────

_ESCALATION_PATTERNS = [
    r"\bменеджер[аеуы]?\b",
    r"\bоператор[аеуы]?\b",
    r"живой\s+человек",
    r"реальн[ыйаяое]+\s+(человек|сотрудник)",
    r"соедин\w*\s+с",
    r"позов\w*\s+(менеджер|оператор)",
    r"\bhuman\b", r"\bagent\b", r"real\s+person",
    r"talk\s+to\s+(a\s+)?(manager|human|agent)",
]


def is_escalation(message: str) -> bool:
    import re
    low = message.lower()
    for p in _ESCALATION_PATTERNS:
        if re.search(p, low):
            return True
    return False


ESCALATION_RESPONSE = (
    "Принято — подключаю менеджера. Я сохранил ваш вопрос и контекст диалога; "
    "живой специалист подхватит в течение рабочего дня. "
    "Если удобно продолжить со мной — задайте ещё вопросы, я рядом."
)


# ── BANT extractor ───────────────────────────────────────────────────────────

_BANT_PROMPT = """Ты — аналитик продаж. Из истории диалога извлеки данные BANT.

История (последние сообщения клиента и ассистента):
{history}

Извлеки из слов КЛИЕНТА:
- budget: озвученный или подразумеваемый бюджет (строка или null)
- authority: роль/должность клиента (строка или null)
- need: главная потребность/боль (строка или null)
- timeline: срок принятия решения/внедрения (строка или null)

Дополнительно:
- qualified: true если есть ≥3 полей — иначе false
- temperature: "hot" (горячий — явно готов купить), "warm" (тёплый — интересуется), "cold" (холодный — разведка)

Ответ ТОЛЬКО в JSON:
{{"budget":..., "authority":..., "need":..., "timeline":..., "qualified":..., "temperature":"..."}}"""


def extract_bant(history_text: str) -> dict[str, Any]:
    """Синхронный LLM-вызов для извлечения BANT. ~150 токенов."""
    client = OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    try:
        resp = client.chat.completions.create(
            model=settings.router_model,
            messages=[{"role": "user", "content": _BANT_PROMPT.format(history=history_text[:3000])}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=250,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
    except Exception as e:
        return {"error": str(e)[:200]}

    # Нормализация
    return {
        "budget":      data.get("budget") or None,
        "authority":   data.get("authority") or None,
        "need":        data.get("need") or None,
        "timeline":    data.get("timeline") or None,
        "qualified":   bool(data.get("qualified", False)),
        "temperature": (data.get("temperature") or "cold").lower(),
    }
