# AI Sales Chatbot на базе CrewAI

Умный чат-бот для продаж: идентифицирует запросы клиентов и маршрутизирует к нужному агенту.

## Архитектура

```
User Message
     ↓
Router (keyword → LLM fallback)     ← лёгкий, дешёвый
     ↓
 greeting/off_topic → прямой ответ (без CrewAI)
 simple_faq/support → Support Agent
 sales_inquiry      → Sales Agent
 product_advice     → Advisor Agent
     ↓
Tools: [RAG Search, CRM Lookup]
     ↓
Memory (SQLite) → история диалогов
     ↓
Response via FastAPI
```

## Быстрый старт

### 1. Установить зависимости

```bash
cd crewAI-main/chatbot_project

# Устанавливаем crewai из локального репозитория
pip install -e ../lib/crewai
pip install -e ../lib/crewai-tools

# Остальные зависимости
pip install -r requirements.txt
```

### 2. Настроить переменные окружения

```bash
cp .env.example .env
# Отредактируй .env — вставь свой API ключ и base_url
```

### 3. Загрузить базу знаний

```bash
python ingest_data.py
# Или с --reset для очистки и перезагрузки:
python ingest_data.py --reset
```

### 4. Запустить сервер

```bash
python main.py
# Сервер запустится на http://localhost:8000
```

### 5. Настроить API туннель (ngrok)

```bash
# Установить ngrok: https://ngrok.com
ngrok http 8000
# Получишь публичный URL вида: https://xxxx.ngrok.io
```

## API

### POST /chat
```json
// Запрос
{
  "message": "Сколько стоит Pro план?",
  "session_id": "user123"   // опционально, создаётся автоматически
}

// Ответ
{
  "response": "Pro план стоит 2490 руб/мес за пользователя...",
  "session_id": "user123",
  "intent": "sales_inquiry",
  "used_llm_for_routing": false
}
```

### GET /history/{session_id}
Возвращает историю диалога сессии.

### DELETE /history/{session_id}
Очищает историю диалога.

### POST /admin/ingest
Переиндексирует базу знаний.

### GET /health
Проверка состояния системы.

## Добавить свою базу знаний

Отредактируй `data/sample_knowledge.json` — формат:

```json
[
  {
    "id": "unique_id",
    "category": "features",    // features | pricing | howto | faq | sales_scripts
    "title": "Название статьи",
    "content": "Текст статьи..."
  }
]
```

Затем запусти переиндексацию:
```bash
python ingest_data.py --file ./data/sample_knowledge.json --reset
```

## Настройка моделей

В `.env`:
```
ORCHESTRATOR_MODEL=gpt-4o        # для сложных цепочек
AGENT_MODEL=gpt-4o-mini          # для агентов (экономия)
ROUTER_MODEL=gpt-4o-mini         # для классификации
EMBEDDING_MODEL=text-embedding-3-small
```

Для локальных моделей (Ollama, LM Studio, vLLM):
```
OPENAI_BASE_URL=http://localhost:11434/v1
OPENAI_API_KEY=ollama
AGENT_MODEL=llama3.2
ROUTER_MODEL=llama3.2
EMBEDDING_MODEL=nomic-embed-text
```

## Добавить свои инструменты агентов

В `tools/` создай новый файл, унаследуйся от `BaseTool`:

```python
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

class MyToolInput(BaseModel):
    query: str = Field(description="...")

class MyTool(BaseTool):
    name: str = "My Tool"
    description: str = "Описание для агента..."
    args_schema: type[BaseModel] = MyToolInput

    def _run(self, query: str) -> str:
        # твоя логика
        return "результат"
```

Затем добавь инструмент в `agents/orchestrator.py` в нужный агент.

## Добавить нового агента

1. Добавь новый интент в `router/intent_router.py` (в `IntentType` и `_KEYWORD_RULES`)
2. Добавь метод `_run_new_agent()` в `agents/orchestrator.py`
3. Добавь ветку в `process()` метод оркестратора

## Тестирование

```bash
# Быстрый тест через curl
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Привет! Расскажи о ваших тарифах", "session_id": "test"}'

# Проверка состояния
curl http://localhost:8000/health

# Swagger UI (интерактивная документация)
open http://localhost:8000/docs
```

## Структура файлов

```
chatbot_project/
├── main.py              # FastAPI приложение
├── config.py            # Настройки (из .env)
├── ingest_data.py       # Загрузка базы знаний
├── requirements.txt
├── .env.example
├── agents/
│   └── orchestrator.py  # CrewAI оркестратор
├── router/
│   └── intent_router.py # Классификатор интентов
├── tools/
│   ├── rag_tool.py      # Поиск по базе знаний
│   └── crm_tool.py      # CRM интеграция
├── memory/
│   └── dialogue_memory.py # SQLite история диалогов
├── knowledge/
│   └── vector_store.py  # ChromaDB обёртка
└── data/
    └── sample_knowledge.json # База знаний
```
