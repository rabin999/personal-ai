"""Real-call harness (plan §3/§4): drive the REAL reasoning engine end-to-end —
real OpenRouter model + real Mongo/Qdrant/Neo4j/Redis — with NO mocks.

`RealTurns` wraps a built `Pipeline` and exposes `say()` to run a real text turn
(the same core loop as voice, minus STT/TTS) capturing the reply + trace, so the
real-call suites can assert on genuine model output the way a human would judge it.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.composition import Pipeline, build_pipeline
from config.settings import get_settings
from core.memory.working import Turn
from core.reasoning.prompt_assembly import DisambiguationRequest
from core.tools.registry import ToolContext
from voice.trace import TraceEmitter, TraceEvent


@dataclass
class TurnResult:
    reply: str
    action: str
    style_flags: list[str]
    trace: list[TraceEvent]


class RecordingTrace(TraceEmitter):
    """Captures every emitted event so a test can inspect the pipeline afterwards."""

    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        self.recorded: list[TraceEvent] = []

    def emit(self, stage, message, *, level="info", **data) -> None:  # type: ignore[override]
        self.recorded.append(
            TraceEvent(
                session_id=self._session_id,  # type: ignore[attr-defined]
                turn=self.current_turn,
                stage=stage,
                message=message,
                level=level,
                data=data,
            )
        )
        super().emit(stage, message, level=level, **data)


class RealTurns:
    """A live pipeline you can hold a real conversation with (text path)."""

    def __init__(self, pipeline: Pipeline, user_id: str = "u_demo_001") -> None:
        self._p = pipeline
        self._user = user_id

    @classmethod
    async def build(cls, user_id: str = "u_demo_001") -> RealTurns:
        return cls(await build_pipeline(get_settings()), user_id)

    async def say(self, text: str, session_id: str) -> TurnResult:
        """Run one real turn through assembly → generation (real model + stores)."""
        trace = RecordingTrace(session_id)
        trace.begin_turn()
        trace.emit("session", "text turn", user_id=self._user, text=text)
        self._p.working.append(session_id, Turn(role="user", text=text))
        prompt = await self._p.assembler.assemble(self._user, session_id, text)
        if isinstance(prompt, DisambiguationRequest):
            reply = "Quick check — " + " or ".join(c.name for c in prompt.candidates[:3]) + "?"
            return TurnResult(
                reply=reply, action="disambiguate", style_flags=[], trace=trace.recorded
            )
        # Representative pipeline spans (mirrors api/routes/chat.py) so the harness
        # trace shows memory-read → assembly → routing → generation, not just the
        # session start. The generator's own per-LLM-call spans go to the pipeline
        # logger; these give a test enough of the turn shape to assert on.
        sections = prompt.sections
        trace.emit(
            "retrieval",
            "memory read before reasoning",
            episodic=[ln for ln in sections.get("episodic", "").splitlines() if ln.strip()],
            semantic_facts=[ln for ln in sections.get("facts", "").splitlines() if ln.strip()],
            entities=[c.name for c in prompt.resolved_entities],
        )
        trace.emit(
            "assembly",
            f"prompt assembled ({len(prompt.system_prompt)} chars)",
            complexity=prompt.complexity_hint,
            prompt_version=prompt.prompt_version,
            prompt_chars=len(prompt.system_prompt),
        )
        trace.emit(
            "router", f"routing to {prompt.complexity_hint} tier", tier=prompt.complexity_hint
        )
        ctx = ToolContext(user_id=self._user, session_id=session_id, project_id=None)
        # Bind the logs so the generator's per-LLM-call / reflection spans persist
        # under this turn (mirrors api/routes/chat.py) — the trace then reconstructs
        # the whole pipeline, not just the stage headers we emit here.
        with self._p.logs.bind(trace_id=session_id, turn_id=1, user_id=self._user):
            result = await self._p.generator.generate(prompt, self._p.dispatcher, ctx)
        trace.emit("generation", f"action={result.action}", action=result.action)
        trace.emit("response", result.final_text, voice_text=result.voice_text or result.final_text)
        trace.emit("session", "turn complete", total_ms=0.0)
        # Persist every span to the durable trace store so a test can reconstruct
        # the turn from the trace alone (Item 6).
        for e in trace.recorded:
            span = e.model_dump()
            span["turn"] = 1
            await self._p.traces.record(self._user, span)
        self._p.working.append(session_id, Turn(role="assistant", text=result.final_text))
        return TurnResult(
            reply=result.final_text,
            action=result.action,
            style_flags=result.style_flags,
            trace=trace.recorded,
        )

    @property
    def llm(self):
        return self._p.llm

    @property
    def episodic(self):
        return self._p.episodic

    @property
    def semantic(self):
        return self._p.semantic

    @property
    def traces(self):
        return self._p.traces

    async def aclose(self) -> None:
        await self._p.aclose()
