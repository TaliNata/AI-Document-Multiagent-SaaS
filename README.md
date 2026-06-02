# AI Document Multiagent SaaS
Production-grade multi-agent system for automated document processing and analysis.

This system ingests business documents (PDF, DOCX, XLSX, scans), extracts structured data, and enables natural language interaction via a chat interface powered by RAG and multi-agent orchestration.

> ⚠️ This is a portfolio version. It includes architecture, key modules, and core logic — not the full proprietary codebase.

---

## What This Project Demonstrates
- Multi-agent orchestration using LangGraph (state machine, not simple chains)
- Real-world RAG pipeline with Qdrant (chunking, embeddings, retrieval)
- Secure multitenant architecture with PostgreSQL Row-Level Security (RLS)
- Production-ready async backend (FastAPI + SQLAlchemy 2.0)
- Background processing (Celery + Redis)
- Multi-model LLM routing (cost optimization across providers)
- End-to-end document pipeline (upload → parse → embed → query)
- **LangSmith observability** — full tracing, evaluation, and monitoring of agent runs
- Handling real-world edge cases in LLM + infrastructure integrations

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Frontend — React 18 + TypeScript + Vite + TailwindCSS        │
│ Chat UI · Document Dashboard · Drag-and-drop Upload          │
├──────────────────────────────────────────────────────────────┤
│ Nginx — TLS 1.3 · Rate Limiting · Security Headers           │
├──────────────────────────────────────────────────────────────┤
│ FastAPI — JWT Auth · RLS Middleware · REST API               │
├───────────────────────┬──────────────────────────────────────┤
│ Celery Workers        │ LangGraph Orchestrator               │
│ • PDF/DOCX/OCR parse  │ • Chat Agent (RAG → LLM)             │
│ • Email intake        │ • Analyst Agent (classify + extract) │
│ • Periodic tasks      │ • Generator Agent (templates)        │
├───────────────────────┴──────────────────────────────────────┤
│ LLM Router (litellm) — Claude Sonnet ↔ GPT-4o-mini           │
│ PII Masking — sensitive data → masked → LLM → unmasked       │
├────────────┬───────────┬───────────┬─────────────────────────┤
│ PostgreSQL │ Qdrant    │ Redis     │ MinIO (S3-compatible)   │
│ + RLS      │ vectors   │ queues    │ file storage            │
└────────────┴───────────┴───────────┴─────────────────────────┘
```

---

## System Flow
1. Document uploaded (API / Telegram / Email)
2. Stored in MinIO
3. Celery task triggered
4. Text extraction (PDF / OCR / DOCX / XLSX)
5. Chunking + embeddings
6. Stored in Qdrant + PostgreSQL
7. Auto-classification (LLM)
8. Chat request → LangGraph orchestrator
9. Routed to appropriate agent
10. Response generated (RAG or structured output)

---

## Observability — LangSmith Integration

The system is fully instrumented with **LangSmith** for production-grade tracing and evaluation.

### What gets traced
Every agent run is captured end-to-end:

```
LangGraph (root)
  ├── classify_intent     — LLM classifies user intent (5.9s)
  ├── route_by_intent     — conditional routing (0.00s)
  └── question / analyze / generate / search — handler node (4.98s)
```

Each trace captures:
- **Input / Output** for every node
- **Token usage and cost** per LLM call
- **Latency** per node and total
- **Sources** — which document chunks were retrieved (RAG)
- **Intent classification** result

### LangSmith trace screenshot

![LangSmith trace](docs/langsmith_trace.png)

*Full graph trace: classify_intent → route_by_intent → question handler. Intent correctly classified, RAG context retrieved (score 0.95), response generated in 11s total.*

### Configuration
```env
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=your_key
LANGCHAIN_PROJECT=ai-doc-agent
LANGCHAIN_ENDPOINT=https://eu.api.smith.langchain.com
```

No code changes required — LangGraph integrates with LangSmith automatically when env vars are set.

---

## Key Engineering Decisions

### 1. Multitenancy via PostgreSQL Row-Level Security (RLS)
Instead of filtering data in application code, isolation is enforced at the database level.

```python
await session.execute(
    text("SET LOCAL app.current_org_id = :org_id"),
    {"org_id": str(org_id)},
)
```
This guarantees that data from different organizations never leaks, even if business logic fails.

### 2. Multi-agent orchestration (LangGraph)
State machine with conditional routing:

```
[START] → classify_intent → route
                              ├─ "question"  → Chat Agent
                              ├─ "analyze"   → Analyst Agent
                              ├─ "generate"  → Generator Agent
                              └─ "search"    → RAG Search
```

Why LangGraph:
- explicit state transitions
- extensible agent system
- retry and persistence support

### 3. Multi-model LLM routing
Different tasks use different models:

```python
doc_type = await call_light(messages)   # cheap model
entities = await call_heavy(messages)   # powerful model
```

Result: 3–5x cost reduction, better performance allocation.

### 4. PII masking before LLM calls
Sensitive data is masked before sending to external APIs and restored afterward.

### 5. Idempotent document processing pipeline
- Safe retries
- Reprocessing without duplication
- Handles PDF, DOCX, XLSX, OCR

---

## Real-World Engineering Challenges
This system was not just generated — it was debugged and brought to a working state.

Key issues resolved:
- SQLAlchemy + asyncpg incompatibility with JSONB casting (`:meta::jsonb`)
- Qdrant client authentication and HTTP/HTTPS mismatch
- LiteLLM integration inconsistencies (API key handling)
- Outdated / incompatible LLM model configurations
- Rate limiter interfering with system diagnostics
- Missing vector collections in Qdrant causing runtime failures
- Handshake protocol edge cases (429 handling, retry logic)

These required debugging across: application → database → vector DB → LLM → infrastructure

---

## Tech Stack

| Layer    | Technologies                                          |
|----------|-------------------------------------------------------|
| Frontend | React 18, TypeScript, Vite, TailwindCSS               |
| Backend  | FastAPI, SQLAlchemy 2.0, asyncpg                      |
| AI       | LangGraph, LangChain, LiteLLM                         |
| LLMs     | Claude Sonnet                                         |
| RAG      | Qdrant, embeddings, chunking                          |
| Parsing  | pymupdf, python-docx, openpyxl, Tesseract             |
| Infra    | Docker Compose, Nginx, Redis, PostgreSQL, MinIO       |
| Observability | LangSmith (tracing, evaluation, monitoring)      |

---

## Security
- JWT authentication + RBAC
- PostgreSQL Row-Level Security
- PII masking before LLM calls
- Redis rate limiting
- Audit logging

---

## Project Structure
```
backend/
  app/
    agents/
    api/
    middleware/
    services/
    integrations/
    models/
    schemas/
frontend/
infra/
deploy/
docker-compose.yml
```

---

## Showcase Modules

| File               | Description                        |
|--------------------|------------------------------------|
| orchestrator.py    | LangGraph orchestration            |
| deps.py            | RLS + dependency injection         |
| pii_masking.py     | PII masking system                 |
| parse_document.py  | Full document pipeline             |

---

## Metrics
- 85 source files
- 10 Docker services
- 4 AI agents
- 12 API endpoints
- 12 PostgreSQL tables with RLS
- ~30 tests
- 3 ingestion channels (web, Telegram, email)
- Full LangSmith observability

---

## Key Takeaway
This project demonstrates:
- building complex AI systems (not just prompts)
- debugging multi-layer architectures
- integrating LLMs into real backend systems
- production observability with LangSmith
- moving from prototype → working system

It reflects production-oriented engineering, not just experimentation.
