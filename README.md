# AI Document Multiagent

**Мультиагентная SaaS-платформа для автоматизации документооборота МСБ**

AI-система, которая принимает документы (PDF, DOCX, XLSX, сканы), автоматически распознаёт, классифицирует, 
извлекает ключевые данные и отвечает на вопросы по ним через чат-интерфейс.

> Это portfolio-версия проекта. Здесь представлена архитектура, ключевые решения и отдельные модули — не полный исходный код.

---

## Архитектура

```
┌──────────────────────────────────────────────────────────────┐
│  Frontend — React 18 + TypeScript + Vite + TailwindCSS       │
│  Chat UI · Document Dashboard · Drag-and-drop Upload         │
├──────────────────────────────────────────────────────────────┤
│  Nginx — TLS 1.3 · Rate Limiting · Security Headers          │
├──────────────────────────────────────────────────────────────┤
│  FastAPI — JWT Auth · RLS Middleware · REST API              │
├───────────────────────┬──────────────────────────────────────┤
│  Celery Workers       │  LangGraph Orchestrator              │
│  • PDF/DOCX/OCR parse │  • Chat Agent (RAG → LLM)            │
│  • Email intake       │  • Analyst Agent (classify + extract)│
│  • Periodic tasks     │  • Generator Agent (templates)       │
├───────────────────────┴──────────────────────────────────────┤
│  LLM Router (litellm) — Claude Sonnet ↔ GPT-4o-mini          │
│  PII Masking — ИНН, паспорт, email → [PLACEHOLDER] → unmask  │
├────────────┬───────────┬───────────┬─────────────────────────┤
│ PostgreSQL │ Qdrant    │ Redis     │  MinIO (S3-compatible)  │
│ 12 tables  │ RAG       │ cache +   │  file storage           │
│ + RLS      │ vectors   │ queues    │                         │
└────────────┴───────────┴───────────┴─────────────────────────┘
```

## Стек технологий

| Слой | Технологии |
|------|-----------|
| Frontend | React 18, TypeScript, Vite, TailwindCSS, Zustand, React Query, react-dropzone |
| Backend API | Python 3.12, FastAPI, SQLAlchemy 2.0, Pydantic v2, asyncpg |
| AI / LLM | LangGraph, LangChain, litellm, Claude Sonnet, GPT-4o-mini |
| RAG Pipeline | OpenAI Embeddings, Qdrant (vector search), RecursiveCharacterTextSplitter |
| Document Parsing | pymupdf, python-docx, openpyxl, Tesseract OCR (rus+eng) |
| Integrations | Telegram Bot API, IMAP/SMTP email intake |
| Infrastructure | Docker Compose (10 services), Nginx, Redis, PostgreSQL 16, MinIO |
| Security | JWT + RBAC, Row-Level Security, PII masking, rate limiting, audit logging, Sentry |
| CI/Testing | pytest, pytest-asyncio, Alembic migrations |

## Ключевые архитектурные решения

### 1. Мультитенантность через Row-Level Security

Вместо фильтрации `WHERE org_id = ...` в каждом запросе — изоляция на уровне PostgreSQL. 
Middleware устанавливает переменную сессии, и RLS автоматически фильтрует все таблицы:

```python
# middleware/tenant.py — извлекает org_id из JWT
class TenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # ... decode JWT, extract org_id ...
        request.state.org_id = uuid.UUID(payload["org_id"])
        return await call_next(request)

# api/deps.py — активирует RLS для транзакции
async def get_db_with_tenant(request: Request):
    async with async_session_factory() as session:
        async with session.begin():
            if org_id:
                await session.execute(
                    text("SET LOCAL app.current_org_id = :org_id"),
                    {"org_id": str(org_id)},
                )
            yield session
```

**Почему это важно:** даже если в коде бизнес-логики забыть фильтрацию по организации, данные не утекут — PostgreSQL сам отфильтрует на уровне движка. 
`SET LOCAL` ограничивает переменную текущей транзакцией — параллельные запросы от других организаций не пересекаются.

### 2. Мультиагентный оркестратор (LangGraph)

State machine с условной маршрутизацией: intent classification (GPT-4o-mini, дёшево) → routing → специализированный агент:

```
[START] → classify_intent → route
                              ├─ "question"  → Chat Agent (RAG → Claude)     → [END]
                              ├─ "analyze"   → Analyst Agent (extract JSON)  → [END]
                              ├─ "generate"  → Generator Agent (templates)   → [END]
                              └─ "search"    → RAG Search (Qdrant)           → [END]
```

**Почему LangGraph, а не if/else:** явный граф состояний с типизированным state, возможность добавлять агентов без рефакторинга, встроенный retry и persistence.

### 3. Мульти-модельный LLM Router с PII-маскированием

Разные задачи → разные модели → оптимизация стоимости в 3-5x:

```python
# Классификация документа — дешёвая модель (~$0.15/1M токенов)
doc_type = await call_light(messages)  # → GPT-4o-mini

# Анализ сложного договора — мощная модель (~$3/1M токенов)
entities = await call_heavy(messages)  # → Claude Sonnet, fallback: GPT-4o
```

Перед каждым вызовом LLM автоматически маскируются персональные данные (ИНН, паспорт, email, телефон → `[ИНН_1]`, `[EMAIL_1]`), после получения ответа — демаскируются обратно. Клиентские данные не уходят в облачные LLM API.

### 4. Идемпотентный пайплайн парсинга

```
Upload → MinIO → Celery task → Extract text → Chunk → Embed → Qdrant + PostgreSQL
                     ↓
              Auto-classify (GPT-4o-mini) + Extract entities (Claude Sonnet)
```

Задача идемпотентна: повторный запуск удаляет старые чанки и пересоздаёт. `max_retries=3` с exponential backoff. 
Поддерживает PDF (с OCR-fallback для сканов), DOCX (параграфы + таблицы), XLSX (все листы), изображения (Tesseract rus+eng).

## Структура проекта

```
ai-doc-agent/                      # 85 файлов, 10 Docker-сервисов
│
├── backend/
│   ├── app/
│   │   ├── agents/                # AI-агенты
│   │   │   ├── orchestrator.py    # LangGraph state machine
│   │   │   ├── chat_agent.py      # RAG → context → LLM → response
│   │   │   ├── analyst_agent.py   # Classify + extract entities (JSON mode)
│   │   │   └── generator_agent.py # Template-based + freeform generation
│   │   │
│   │   ├── api/                   # FastAPI endpoints
│   │   │   ├── auth.py            # Register, login, JWT refresh
│   │   │   ├── documents.py       # Upload, list, get, delete
│   │   │   ├── chat.py            # Send message → orchestrator → response
│   │   │   ├── chat_stream.py     # SSE streaming (token-by-token)
│   │   │   ├── deps.py            # DB+RLS, CurrentUser, RoleRequired
│   │   │   └── webhooks.py        # Telegram webhook receiver
│   │   │
│   │   ├── middleware/
│   │   │   ├── tenant.py          # Multitenancy (JWT → org_id → request.state)
│   │   │   ├── rate_limit.py      # Redis sliding window (per-user, per-org)
│   │   │   └── audit.py           # Log all mutations to audit_log
│   │   │
│   │   ├── services/
│   │   │   ├── llm_router.py      # Multi-model: Claude ↔ GPT-4o ↔ mini
│   │   │   ├── pii_masking.py     # Mask ИНН/passport/email before LLM calls
│   │   │   ├── parser.py          # PDF/DOCX/XLSX/OCR text extraction
│   │   │   ├── chunker.py         # RecursiveCharacterTextSplitter (800/200)
│   │   │   ├── embeddings.py      # OpenAI → Qdrant vector storage
│   │   │   ├── rag.py             # Semantic search + context builder
│   │   │   └── storage.py         # MinIO (S3-compatible) file operations
│   │   │
│   │   ├── integrations/
│   │   │   ├── telegram_bot.py    # Upload docs, ask questions, approve
│   │   │   ├── telegram_linking.py# Account binding via /start <token>
│   │   │   └── email_intake.py    # IMAP poll → extract attachments → parse
│   │   │
│   │   ├── models/                # SQLAlchemy 2.0 ORM
│   │   ├── schemas/               # Pydantic v2 request/response
│   │   ├── config.py              # Pydantic Settings (env-based)
│   │   ├── database.py            # Async engine + session factory
│   │   └── worker.py              # Celery + Beat configuration
│   │
│   ├── tests/                     # pytest + pytest-asyncio (~30 tests)
│   └── alembic/                   # Database migrations
│
├── frontend/
│   └── src/
│       ├── pages/
│       │   ├── ChatPage.tsx       # AI chat with sources + entities
│       │   ├── DocumentsPage.tsx  # Drag-and-drop + status badges
│       │   └── LoginPage.tsx      # Auth with register toggle
│       ├── lib/
│       │   ├── api.ts             # Typed fetch wrapper for all endpoints
│       │   └── auth-store.ts      # Zustand auth state management
│       └── App.tsx                # Routing + sidebar + auth guard
│
├── infra/
│   ├── nginx/nginx.conf           # Reverse proxy + SSL + rate limiting
│   └── init-db.sql                # 12 tables + RLS policies + indexes
│
├── deploy/
│   ├── setup-server.sh            # One-command VPS setup
│   ├── deploy.sh                  # SSL + build + start (one command)
│   └── backup.sh                  # PostgreSQL dump + MinIO + .env
│
├── docker-compose.yml             # 10 services
├── docker-compose.prod.yml        # Production overrides
└── generate_env.py                # Secure password generator
```

## Showcase-модули

В папке [`/showcase`](./showcase) — ключевые модули с подробными комментариями:

| Файл | Что демонстрирует |
|------|--------------------|
| [`orchestrator.py`](./showcase/orchestrator.py) | LangGraph state machine — intent routing → agent dispatch |
| [`deps.py`](./showcase/deps.py) | FastAPI DI — RLS activation, user extraction, role checker |
| [`pii_masking.py`](./showcase/pii_masking.py) | Regex-based PII detection + reversible masking |
| [`parse_document.py`](./showcase/parse_document.py) | Celery task — full idempotent pipeline with retry |

## Метрики проекта

- **85 файлов** исходного кода
- **10 Docker-сервисов** в compose
- **4 AI-агента** через LangGraph
- **12 эндпоинтов** API (REST + SSE streaming)
- **12 таблиц** PostgreSQL с RLS
- **~30 тестов** (pytest)
- **3 канала приёма** документов: web upload, Telegram bot, email

---
