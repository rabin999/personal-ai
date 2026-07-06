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
from core.reasoning.prompt_assembly import DisambiguationRequest
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

    pipeline.working.append(body.session_id, Turn(role="user", text=body.text))
    prompt = await pipeline.assembler.assemble(user.user_id, body.session_id, body.text)
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

    result = await pipeline.generator.generate(prompt)
    trace.emit("generation", f"action={result.action}", action=result.action)
    trace.emit("response", result.final_text)
    pipeline.working.append(body.session_id, Turn(role="assistant", text=result.final_text))

    trace.close()
    events = [event async for event in trace.events()]
    return ChatResponse(
        reply=result.final_text,
        action=result.action,
        turn_id=result.turn_id,
        trace=events,
    )
