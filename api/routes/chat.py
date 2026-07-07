"""Text chat route (spec §0.6): a typed conversation turn, no audio.

The same reasoning core as voice (§10 assembly → §11/§12 generation with
behavior gates) without STT/TTS — a text fallback and the simplest way to
exercise the assembled pipeline. Returns the reply plus the full stage trace
so the UI's log sidebar works for typed turns too.
"""

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from api.composition import Pipeline
from api.deps import CurrentUser
from core.memory.working import Turn
from core.reasoning.prompt_assembly import AssembledPrompt, DisambiguationRequest
from core.tools.registry import ToolContext
from voice.trace import TraceEmitter, TraceEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# Per-session verbatim turn counters for the durable conversation log (§6).
_turn_counters: dict[str, int] = {}


class ChatRequest(BaseModel):
    text: str
    session_id: str = "text_session"


class ChatResponse(BaseModel):
    reply: str
    action: str
    turn_id: str | None = None
    trace: list[TraceEvent]


@router.post("/chat")
async def chat(body: ChatRequest, user: CurrentUser, request: Request) -> ChatResponse:
    pipeline: Pipeline = request.app.state.pipeline
    trace = TraceEmitter(body.session_id)
    trace.emit("session", "text turn started", user_id=user.user_id)

    # Pull-at-pause (§14): results of prior background tasks (e.g. a web search)
    # get delivered now, in the companion's voice, dropped if no longer relevant.
    recent = " ".join(t.text for t in pipeline.working.recent(body.session_id, n=4))
    deliveries = await pipeline.delivery.deliveries_for_pause(body.session_id, user.user_id, recent)
    for d in deliveries:
        trace.emit("response", d.line, delivered=True)

    pipeline.working.append(body.session_id, Turn(role="user", text=body.text))
    prompt = await pipeline.assembler.assemble(user.user_id, body.session_id, body.text)
    context = _tool_context(user.user_id, body.session_id, prompt)
    if isinstance(prompt, DisambiguationRequest):
        trace.emit(
            "assembly",
            "ambiguous reference — disambiguating",
            candidates=[c.name for c in prompt.candidates[:3]],
        )
    else:
        trace.emit(
            "assembly",
            f"prompt assembled (complexity={prompt.complexity_hint})",
            complexity=prompt.complexity_hint,
            entities=[c.name for c in prompt.resolved_entities],
        )
        trace.emit("router", f"routing to {prompt.complexity_hint} tier")

    with pipeline.logs.bind(
        trace_id=body.session_id, turn_id=trace.current_turn, user_id=user.user_id
    ):
        pipeline.logs.info("turn.request", text=body.text)
    result = await pipeline.generator.generate(prompt, pipeline.dispatcher, context)
    with pipeline.logs.bind(
        trace_id=body.session_id, turn_id=trace.current_turn, user_id=user.user_id
    ):
        pipeline.logs.info(
            "turn.response",
            action=result.action,
            style_flags=result.style_flags,
            reply_chars=len(result.final_text),
        )
    trace.emit("generation", f"action={result.action}", action=result.action)
    if result.style_flags:  # §7: tone regression is visible, not silent
        trace.emit(
            "generation",
            f"style warning: forbidden assistant-speak {result.style_flags}",
            level="warn",
            style_flags=result.style_flags,
        )
    trace.emit("response", result.final_text)
    pipeline.working.append(body.session_id, Turn(role="assistant", text=result.final_text))

    # Memory parity with the voice runtime: the text path also persists the turn
    # to episodic memory (§5, cross-session recall) and the durable conversation
    # log (§6), best-effort so it never blocks the reply.
    await _persist_turn(
        pipeline, user.user_id, body.session_id, body.text, result.final_text, trace
    )

    # Any background result that landed during this turn is prepended to the reply.
    reply = " ".join([*(d.line for d in deliveries), result.final_text]).strip()

    trace.close()
    events = [event async for event in trace.events()]
    return ChatResponse(
        reply=reply,
        action=result.action,
        turn_id=result.turn_id,
        trace=events,
    )


async def _persist_turn(
    pipeline: Pipeline,
    user_id: str,
    session_id: str,
    user_text: str,
    assistant_text: str,
    trace: TraceEmitter,
) -> None:
    """Run the memory-extraction write step + the durable conversation log (§1/§6)."""
    try:
        # §1 WRITE step: an explicit extraction decides what/where to persist
        # (episodic events, distilled semantic facts, trades) — not a blind dump.
        extracted = await pipeline.extractor.extract_and_store(
            user_id, session_id, user_text, assistant_text
        )
        if extracted.episodic_written or extracted.semantic_written or extracted.trades_written:
            trace.emit(
                "memory",
                f"stored {extracted.episodic_written} event(s), "
                f"{extracted.semantic_written} fact(s), {extracted.trades_written} trade(s)",
                episodic=extracted.events,
                semantic=extracted.facts,
                trades=extracted.trades_written,
            )
    except Exception:
        logger.exception("memory extraction failed (text turn)")
    try:
        _turn_counters[session_id] = _turn_counters.get(session_id, 0) + 1
        await pipeline.conversations.record_turn(
            user_id=user_id,
            session_id=session_id,
            turn_index=_turn_counters[session_id],
            user_text=user_text,
            assistant_text=assistant_text,
            trace_turn=trace.current_turn,
        )
    except Exception:
        logger.exception("conversation persistence failed (text turn)")


def _tool_context(user_id: str, session_id: str, prompt: object) -> ToolContext:
    """Build the tool context; a resolved project entity scopes project tools (§8.4)."""
    project_id = None
    if isinstance(prompt, AssembledPrompt):
        for c in prompt.resolved_entities:
            if c.entity_type == "project":
                project_id = c.entity_id
                break
    return ToolContext(user_id=user_id, session_id=session_id, project_id=project_id)
