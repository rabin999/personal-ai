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
    deliveries = await pipeline.delivery.deliveries_for_pause(
        body.session_id, user.user_id, recent
    )
    for d in deliveries:
        trace.emit("response", d.line, delivered=True)

    pipeline.working.append(body.session_id, Turn(role="user", text=body.text))
    prompt = await pipeline.assembler.assemble(user.user_id, body.session_id, body.text)
    context = _tool_context(user.user_id, body.session_id, prompt)
    if isinstance(prompt, DisambiguationRequest):
        trace.emit(
            "assembly", "ambiguous reference — disambiguating",
            candidates=[c.name for c in prompt.candidates[:3]],
        )
    else:
        trace.emit(
            "assembly", f"prompt assembled (complexity={prompt.complexity_hint})",
            complexity=prompt.complexity_hint,
            entities=[c.name for c in prompt.resolved_entities],
        )
        trace.emit("router", f"routing to {prompt.complexity_hint} tier")

    result = await pipeline.generator.generate(prompt, pipeline.dispatcher, context)
    trace.emit("generation", f"action={result.action}", action=result.action)
    trace.emit("response", result.final_text)
    pipeline.working.append(body.session_id, Turn(role="assistant", text=result.final_text))

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


def _tool_context(user_id: str, session_id: str, prompt: object) -> ToolContext:
    """Build the tool context; a resolved project entity scopes project tools (§8.4)."""
    project_id = None
    if isinstance(prompt, AssembledPrompt):
        for c in prompt.resolved_entities:
            if c.entity_type == "project":
                project_id = c.entity_id
                break
    return ToolContext(user_id=user_id, session_id=session_id, project_id=project_id)
