"""
Memory Agent API — FastAPI service.
Endpoints: /session_init, /ask, /teach, /stats
"""
import os
import sys
import structlog
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from memory_agent.memory import MemoryStore
from memory_agent.llm import LLMRouter, build_system_prompt, LLMResponse

# ── Models ─────────────────────────────────────────────────

class SessionInitRequest(BaseModel):
    task: str
    role: str = ""
    max_rules: int = Field(default=10, ge=1, le=30)

class SessionInitResponse(BaseModel):
    memories: list[dict]
    context_prompt: str
    memory_count: int

class AskRequest(BaseModel):
    task: str
    message: str
    role: str = ""
    model: str = "gemini"
    max_rules: int = Field(default=10, ge=1, le=30)
    thread_history: str = ""

class AskResponse(BaseModel):
    response: str
    model_used: str
    memories_used: int
    usage: dict | None = None

class TeachRequest(BaseModel):
    domain: str
    rule_type: str = "convention"
    rule: str
    example: str = ""
    confidence: int = Field(default=7, ge=1, le=10)

class TeachResponse(BaseModel):
    id: str
    domain: str
    confidence: int
    message: str

class StatsResponse(BaseModel):
    total_rules: int
    domains: dict
    backend: str

# ── App lifecycle ──────────────────────────────────────────

logger = structlog.get_logger()

store: MemoryStore | None = None
router: LLMRouter | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store, router
    data_dir = os.environ.get("LOOM_DATA_DIR", str(Path.home() / ".loom-agent"))
    store = MemoryStore(data_dir=Path(data_dir))
    router = LLMRouter()
    logger.info("memory_agent_started", backend=store.stats()["backend"])
    yield
    logger.info("memory_agent_shutdown")


app = FastAPI(
    title="Loom Memory Agent",
    description="Shared memory for AI agents. teach → store → recall next session.",
    version="0.1.0",
    lifespan=lifespan,
)

# ── Endpoints ──────────────────────────────────────────────

@app.post("/session_init", response_model=SessionInitResponse)
async def session_init(req: SessionInitRequest):
    """Load relevant memories and build context for a new task."""
    if store is None:
        raise HTTPException(500, "Store not initialized")

    memories = store.get_context(task=req.task, role=req.role, max_rules=req.max_rules)
    context_prompt = build_system_prompt(req.task, memories, req.role)

    return SessionInitResponse(
        memories=[{"id": m.id, "domain": m.domain, "rule_type": m.rule_type,
                    "rule": m.rule, "confidence": m.confidence}
                  for m in memories],
        context_prompt=context_prompt,
        memory_count=len(memories),
    )


@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest):
    """Full pipeline: load context → call LLM → return response."""
    if store is None or router is None:
        raise HTTPException(500, "Not initialized")

    # 1. Load relevant memories
    memories = store.get_context(task=req.task, role=req.role, max_rules=req.max_rules)

    # 2. Build system prompt with injected memories + thread history
    system_prompt = build_system_prompt(req.task, memories, req.role, req.thread_history)

    # 3. Route to LLM
    llm = LLMRouter(model=req.model)
    try:
        response = await llm.ask(
            system_prompt=system_prompt,
            user_message=req.message,
        )
    except Exception as e:
        logger.error("llm_call_failed", error=str(e), model=req.model)
        raise HTTPException(502, f"LLM call failed: {e}")

    logger.info("ask_complete", model=response.model,
                memories_used=len(memories),
                tokens=response.usage.get("total_tokens", 0) if response.usage else 0)

    return AskResponse(
        response=response.content,
        model_used=response.model,
        memories_used=len(memories),
        usage=response.usage,
    )


@app.post("/teach", response_model=TeachResponse)
async def teach(req: TeachRequest):
    """Store a new rule. Bumps confidence on duplicates."""
    if store is None:
        raise HTTPException(500, "Store not initialized")

    if not req.rule.strip():
        raise HTTPException(400, "Rule text is required")

    memory = store.teach(
        domain=req.domain,
        rule_type=req.rule_type,
        rule=req.rule.strip(),
        example=req.example,
        confidence=req.confidence,
    )

    logger.info("teach_stored", domain=memory.domain, confidence=memory.confidence)

    return TeachResponse(
        id=memory.id,
        domain=memory.domain,
        confidence=memory.confidence,
        message=f"Stored in {memory.domain}. Confidence: {memory.confidence}/10.",
    )


@app.get("/stats", response_model=StatsResponse)
async def stats():
    """Memory store statistics."""
    if store is None:
        raise HTTPException(500, "Store not initialized")
    s = store.stats()
    return StatsResponse(total_rules=s["total_rules"], domains=s["domains"], backend=s["backend"])


@app.get("/health")
async def health():
    return {"status": "ok", "backend": store.stats()["backend"] if store else "unknown"}
