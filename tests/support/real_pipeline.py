"""Real-call harness (plan §3/§4): drive the REAL reasoning engine end-to-end —
real OpenRouter model + real Mongo/Qdrant/Neo4j/Redis — with NO mocks.

`RealTurns` wraps a built `Pipeline` and exposes three drivers over the SAME assembled
prompt, so a test can ask what changes when only the caller changes (E5):

    say()         → `orchestrator.generate()`        what `api/routes/chat.py` runs
    say_spoken()  → `orchestrator.generate_spoken()` what `voice/session.py` runs, minus
                    STT/TTS — the engine boundary, not the transducers around it
    say_voice()   → `VoiceSession.converse()`        the whole live path incl. audio

`say` and `say_spoken` differ in exactly one line: which method of the wired engine they
call. Any behavioural difference between their results is therefore a property of the
engine, not of the harness — which is what makes caller-independence testable at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from api.composition import Pipeline, build_pipeline
from config.settings import get_settings
from core.memory.working import Turn
from core.reasoning.prompt_assembly import DisambiguationRequest
from core.tools.registry import ToolContext
from voice.trace import TraceEmitter, TraceEvent

if TYPE_CHECKING:
    from scripts.live_turn import TurnCapture


@dataclass
class TurnResult:
    reply: str
    action: str
    style_flags: list[str]
    trace: list[TraceEvent]
    # The DURABLE spans for this turn, read back from the trace store after the turn
    # completes. `trace` above holds only what this harness emitted; the engine's own
    # spans — every LLM call, every tool, the reflection step — land in the store via
    # the bound logger, and this is the only place a test can see them.
    spans: list[dict[str, Any]] = field(default_factory=list)
    # E5: the sentences handed to TTS, in order. Empty on the text path. A spoken turn
    # that ends with `spoken == []` means the user heard silence.
    spoken: list[str] = field(default_factory=list)

    def _stage(self, stage: str) -> list[dict[str, Any]]:
        return [s for s in self.spans if s.get("stage") == stage]

    @property
    def searches(self) -> list[str]:
        """DISTINCT `web_search` queries actually issued. The tool stage emits several
        spans per search (dispatcher `phase=request` plus the capability-repair backstop),
        so a naive count double-reports."""
        seen: list[str] = []
        for span in self._stage("tool"):
            data = span.get("data") or {}
            if data.get("tool") != "web_search":
                continue
            query = str((data.get("args") or {}).get("query") or "").strip()
            if query and query not in seen:
                seen.append(query)
        return seen

    @property
    def reflected(self) -> bool:
        """Did the §9.3 self-reflection step run? Read from the trace, never assumed."""
        return bool(self._stage("reflection"))

    @property
    def purposes(self) -> list[str]:
        return [str((s.get("data") or {}).get("purpose")) for s in self._stage("llm")]

    def graph_node(self, node: str) -> dict[str, Any]:
        """The envelope the named orchestrator graph node wrote, or {}."""
        for span in self._stage("reasoning"):
            data = span.get("data") or {}
            if data.get("node") == node:
                return data
        return {}


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
    """A live pipeline you can hold a real conversation with.

    ``say`` drives the TEXT path (``orchestrator.generate``) — what ``api/routes/chat.py``
    runs. ``say_voice`` drives the VOICE path (``VoiceSession.converse``) — what
    ``api/routes/voice.py`` runs. They are DIFFERENT code paths (docs/CODE_FLOW.md §0), and
    for a long time only the text one was ever tested, which is how a TypeError that silenced
    every voice turn coexisted with a green real-call suite (F4).
    """

    def __init__(self, pipeline: Pipeline, user_id: str = "u_demo_001") -> None:
        self._p = pipeline
        self._user = user_id
        self._turns: dict[str, int] = {}  # per-session turn counter (trace fidelity)

    @classmethod
    async def build(cls, user_id: str = "u_demo_001") -> RealTurns:
        return cls(await build_pipeline(get_settings()), user_id)

    @property
    def pipeline(self) -> Pipeline:
        return self._p

    async def say_voice(self, text: str, **kwargs: Any) -> TurnCapture:
        """One real turn through the LIVE voice entrypoint (VAD → endpointing → STT →
        orchestrator → TTS)."""
        from scripts.live_turn import drive_turn

        return await drive_turn(self._p, self._user, text, **kwargs)

    async def say_spoken(self, text: str, session_id: str) -> TurnResult:
        """One real turn through `orchestrator.generate_spoken()` — the engine method the
        voice edge calls — capturing the sentences it hands to TTS.

        STT and TTS are deliberately absent: they are transducers around the engine, and
        this harness exists to isolate the engine's DECISIONS from them. Compare against
        `say()` on the same utterance to test caller independence (E5).
        """
        return await self._run(text, session_id, spoken=True)

    async def say(self, text: str, session_id: str) -> TurnResult:
        """One real turn through `orchestrator.generate()` — the engine method the text
        edge (`api/routes/chat.py`) calls."""
        return await self._run(text, session_id, spoken=False)

    async def _run(self, text: str, session_id: str, *, spoken: bool) -> TurnResult:
        """Run one real turn through assembly → generation (real model + stores)."""
        # Real per-session turn number (mirrors api/routes/chat.py) so each turn's
        # spans — incl. the graph-node reasoning + per-LLM-call spans bound below —
        # land under a DISTINCT turn in the durable store, not all collapsed to 1.
        self._turns[session_id] = self._turns.get(session_id, 0) + 1
        turn_no = self._turns[session_id]
        trace = RecordingTrace(session_id)
        for _ in range(turn_no):
            trace.begin_turn()  # advance the emitter to this turn index
        trace.emit(
            "session", "spoken turn" if spoken else "text turn", user_id=self._user, text=text
        )
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
            recall_source=prompt.recall_source,  # F3/F4 conversation-recall routing
        )
        trace.emit(
            "assembly",
            f"prompt assembled ({len(prompt.system_prompt)} chars)",
            complexity=prompt.complexity_hint,
            prompt_version=prompt.prompt_version,
            prompt_chars=len(prompt.system_prompt),
            active_traits=[f"{t['id']}:v{t['version']}" for t in prompt.active_traits],
            trait_text=sections.get("traits", ""),
            system_prompt=prompt.system_prompt,  # F7: full verbatim prompt
            messages=prompt.messages,
            recall_source=prompt.recall_source,
            user_context_signals=prompt.user_context_signals,  # C5 signals used
        )
        trace.emit(
            "router", f"routing to {prompt.complexity_hint} tier", tier=prompt.complexity_hint
        )
        ctx = ToolContext(user_id=self._user, session_id=session_id, project_id=None)
        spoken_sentences: list[str] = []

        async def speak(sentence: str) -> None:
            spoken_sentences.append(sentence)

        # Bind the logs so the generator's per-LLM-call / reflection spans persist
        # under this turn (mirrors api/routes/chat.py) — the trace then reconstructs
        # the whole pipeline, not just the stage headers we emit here.
        with self._p.logs.bind(trace_id=session_id, turn_id=turn_no, user_id=self._user):
            # Exercise the WIRED engine (LangGraph orchestrator by default) — not
            # the native generator directly — so tests judge the real turn engine.
            engine = self._p.orchestrator
            if spoken:
                result = await engine.generate_spoken(prompt, self._p.dispatcher, ctx, speak)
            else:
                result = await engine.generate(prompt, self._p.dispatcher, ctx)
        trace.emit("generation", f"action={result.action}", action=result.action)
        trace.emit("response", result.final_text, voice_text=result.voice_text or result.final_text)
        trace.emit("session", "turn complete", total_ms=0.0)
        # Persist every span to the durable trace store so a test can reconstruct
        # the turn from the trace alone (Item 6).
        for e in trace.recorded:
            span = e.model_dump()
            span["turn"] = turn_no
            await self._p.traces.record(self._user, span)
        self._p.working.append(session_id, Turn(role="assistant", text=result.final_text))
        # Read the DURABLE spans back: the engine's own spans (llm / tool / reflection /
        # graph-node) went to the bound logger, not to `trace.recorded`. Without this a
        # test can only see the reply — which is how "did self-reflection run?" stayed
        # unanswerable for so long.
        spans = await self._p.traces.traces_for(self._user, session_id)
        return TurnResult(
            reply=result.final_text,
            action=result.action,
            style_flags=result.style_flags,
            trace=trace.recorded,
            spans=[s for s in spans if s.get("turn") in (turn_no, None)],
            spoken=spoken_sentences,
        )

    @property
    def user_id(self) -> str:
        return self._user

    @property
    def assembler(self):
        return self._p.assembler

    @property
    def profiles(self):
        return self._p.profiles

    @property
    def compactor(self):
        return self._p.compactor

    @property
    def working(self):
        return self._p.working

    @property
    def conversations(self):
        return self._p.conversations

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
