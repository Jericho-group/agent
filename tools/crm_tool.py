"""
CRM Tool — заглушка для подключения к CRM системе.

Сейчас хранит данные в памяти (dict). В продакшене замени
_get_client_data() на реальный запрос к вашей CRM (Bitrix24, AmoCRM, HubSpot и т.д.)
"""

from pydantic import BaseModel, Field

from crewai.tools import BaseTool

# ── Тестовые данные (замени на реальный CRM-запрос) ─────────────────────────

_MOCK_CLIENTS: dict[str, dict] = {
    "client_001": {
        "name": "Иван Петров",
        "company": "ООО Ромашка",
        "plan": "Basic",
        "since": "2024-01-15",
        "status": "active",
        "notes": "Интересовался переходом на Pro план. Основная боль: не хватает API.",
    },
    "client_002": {
        "name": "Anna Schmidt",
        "company": "TechCorp GmbH",
        "plan": "Pro",
        "since": "2023-06-01",
        "status": "active",
        "notes": "Enterprise клиент. Использует интеграцию с Zapier.",
    },
}


class CRMLookupInput(BaseModel):
    session_id: str = Field(
        description="ID сессии или клиента для поиска данных в CRM."
    )


class CRMTool(BaseTool):
    name: str = "CRM Client Lookup"
    description: str = (
        "Получает информацию о клиенте из CRM системы: "
        "текущий тарифный план, историю, заметки менеджера. "
        "Используй чтобы персонализировать ответы и предложения."
    )
    args_schema: type[BaseModel] = CRMLookupInput

    def _run(self, session_id: str) -> str:
        # В продакшене: вызов к API твоей CRM
        client = _MOCK_CLIENTS.get(session_id)

        if not client:
            return (
                "Клиент не найден в CRM. "
                "Это новый лид — персонализированных данных нет."
            )

        return (
            f"Клиент: {client['name']} ({client['company']})\n"
            f"Тариф: {client['plan']} | Статус: {client['status']}\n"
            f"Клиент с: {client['since']}\n"
            f"Заметки: {client['notes']}"
        )
