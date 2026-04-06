"""
LangGraph Orchestrator — the brain of the system.

Implements a state machine that:
1. Classifies user intent (question, generate, analyze, search)
2. Routes to the appropriate agent
3. Returns the result

Graph flow:

    [START] → classify_intent → route
                                  ├─ "question"  → chat_agent     → [END]
                                  ├─ "analyze"   → analyst_agent   → [END]
                                  ├─ "generate"  → generator_agent → [END]
                                  └─ "search"    → rag_search      → [END]

Why LangGraph and not simple if/else:
- State is explicit and inspectable (debugging)
- Easy to add new agents (add node + edge)
- Supports conditional routing, loops, human-in-the-loop
- Built-in state persistence (can resume interrupted flows)
"""

import logging
from typing import TypedDict, Literal

from langgraph.graph import StateGraph, END

from app.services.llm_router import call_light

logger = logging.getLogger(__name__)


# ─── State schema ─────────────────────────────────────────────
class AgentState(TypedDict):
    """State passed between nodes in the graph."""

    # Input
    user_message: str
    org_id: str
    document_id: str | None
    conversation_history: list[dict]

    # Routing
    intent: str  # question, analyze, generate, search

    # Output
    response: str
    sources: list[dict]
    extracted_entities: dict


# ─── Node: Classify intent ────────────────────────────────────
INTENT_PROMPT = """
Classify the user request into ONE of the following intents:

1. search — user is asking a question about documents
2. extract — user wants structured data extracted (fields, entities)
3. summarize — user wants a summary of a document
4. generate — user wants content generation (text, response, rewrite)
5. general — general question not tied to documents

Return ONLY valid JSON:

{
  "intent": "search | extract | summarize | generate | general"
}

Rules:
- Do not explain your choice
- Do not add extra fields
- Always return exactly one intent
"""


async def classify_intent(state: AgentState) -> AgentState:
    """Determine what the user wants to do."""
    messages = [
        {"role": "system", "content": INTENT_PROMPT},
        {"role": "user", "content": state["user_message"]},
    ]

    intent = await call_light(messages, temperature=0.0, max_tokens=10)
    intent = intent.strip().lower()

    valid_intents = {"question", "analyze", "generate", "search"}
    if intent not in valid_intents:
        intent = "question"  # default fallback

    logger.info(
        f"Intent classified: '{intent}' for message: '{state['user_message'][:50]}...'"
    )

    return {**state, "intent": intent}


# ─── Node: Chat (answer questions) ───────────────────────────
async def handle_question(state: AgentState) -> AgentState:
    """Answer a question using RAG context."""
    from app.agents.chat_agent import chat_respond

    result = await chat_respond(
        user_message=state["user_message"],
        org_id=state["org_id"],
        conversation_history=state.get("conversation_history"),
        document_id=state.get("document_id"),
    )

    return {
        **state,
        "response": result["response"],
        "sources": result["sources"],
    }


# ─── Node: Analyze (extract entities) ────────────────────────
async def handle_analyze(state: AgentState) -> AgentState:
    """Analyze a document — extract entities and classify."""
    from app.agents.analyst_agent import extract_entities
    from app.agents.chat_agent import chat_respond

    # First, get the document text via RAG
    result = await chat_respond(
        user_message=state["user_message"],
        org_id=state["org_id"],
        document_id=state.get("document_id"),
    )

    # If we have a specific document, extract entities from its chunks
    entities = {}
    if result["sources"]:
        full_text = "\n".join(s["snippet"] for s in result["sources"])
        entities = await extract_entities(full_text)

    return {
        **state,
        "response": result["response"],
        "sources": result["sources"],
        "extracted_entities": entities,
    }


# ─── Node: Generate ──────────────────────────────────────────
async def handle_generate(state: AgentState) -> AgentState:
    """Generate a document from user instructions."""
    from app.agents.generator_agent import generate_from_description
    from app.services.rag import search_documents, build_context

    # Search for relevant context (existing docs as reference)
    search_results = await search_documents(
        query=state["user_message"],
        org_id=state["org_id"],
        top_k=3,
    )
    context = build_context(search_results, max_chars=3000)

    generated_text = await generate_from_description(
        description=state["user_message"],
        context=context if search_results else "",
    )

    sources = [
        {
            "document_id": r.document_id,
            "chunk_index": r.chunk_index,
            "score": round(r.score, 3),
            "snippet": r.content[:150],
        }
        for r in search_results
    ]

    return {
        **state,
        "response": generated_text,
        "sources": sources,
    }


# ─── Node: Search ────────────────────────────────────────────
async def handle_search(state: AgentState) -> AgentState:
    """Search for documents and return summaries."""
    from app.services.rag import search_documents, build_context

    results = await search_documents(
        query=state["user_message"],
        org_id=state["org_id"],
        top_k=10,
    )

    if results:
        context = build_context(results)
        response = f"Найдено {len(results)} релевантных фрагментов:\n\n{context}"
    else:
        response = "По вашему запросу ничего не найдено в базе документов."

    sources = [
        {
            "document_id": r.document_id,
            "chunk_index": r.chunk_index,
            "score": round(r.score, 3),
            "snippet": r.content[:150],
        }
        for r in results
    ]

    return {
        **state,
        "response": response,
        "sources": sources,
    }


# ─── Router ──────────────────────────────────────────────────
def route_by_intent(
    state: AgentState,
) -> Literal["question", "analyze", "generate", "search"]:
    """Conditional edge — routes to the correct handler node."""
    return state["intent"]


# ─── Build the graph ─────────────────────────────────────────
def build_orchestrator() -> StateGraph:
    """
    Construct the LangGraph state machine.

    Returns a compiled graph ready for `.ainvoke(state)`.
    """
    graph = StateGraph(AgentState)

    # Add nodes
    graph.add_node("classify_intent", classify_intent)
    graph.add_node("question", handle_question)
    graph.add_node("analyze", handle_analyze)
    graph.add_node("generate", handle_generate)
    graph.add_node("search", handle_search)

    # Entry point
    graph.set_entry_point("classify_intent")

    # Conditional routing after intent classification
    graph.add_conditional_edges(
        "classify_intent",
        route_by_intent,
        {
            "question": "question",
            "analyze": "analyze",
            "generate": "generate",
            "search": "search",
        },
    )

    # All handlers lead to END
    graph.add_edge("question", END)
    graph.add_edge("analyze", END)
    graph.add_edge("generate", END)
    graph.add_edge("search", END)

    return graph.compile()


# Singleton — compiled once, reused across requests
orchestrator = build_orchestrator()
