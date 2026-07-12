"""F3 — programming errors surface loudly; dependency failures degrade gracefully.

A broad ``except Exception`` on the voice turn path absorbed a real ``TypeError`` and
turned it into silence on every turn (docs/CODE_FLOW.md §0). These tests pin the two
halves of the contract so it cannot regress:

- a BUG in our own code (TypeError/AttributeError/...) must propagate out of the
  conversation AND be recorded as a failed step in the trace;
- an EXTERNAL dependency failure (LLM/STT/TTS/search/store) must NOT kill the
  conversation — the companion says honestly that the step failed and keeps listening.
"""

from collections.abc import AsyncIterator

import pytest

from core.memory.working import WorkingMemory
from core.reasoning.orchestrator import (
    OrchestratorContractError,
    assert_orchestrator_contract,
)
from core.reasoning.response_gen import GenerationResult
from ports.llm import LLMUnavailable
from tests.unit.test_voice_session import (
    SESSION,
    FakeAssembler,
    FakeSTT,
    FakeTTS,
    ScriptedVAD,
    _frames,
)
from voice.endpointing import SemanticEndpointer
from voice.pipeline import PipelineConfig
from voice.session import VoiceSession
from voice.trace import TraceEmitter

USER = "u_demo_001"

# One utterance then enough trailing silence for the endpointer to commit the turn.
_SPEECH_THEN_SILENCE = [0.9] * 6 + [0.02] * 20


class BuggyGenerator:
    """Stands in for a mis-wired engine: raises the exact defect that shipped."""

    async def generate(
        self, prompt: object, dispatcher: object = None, context: object = None
    ) -> GenerationResult:
        raise TypeError("generate_spoken() got an unexpected keyword argument 'temperature'")

    async def generate_spoken(
        self, prompt: object, dispatcher: object, context: object, speak: object, **_kw: object
    ) -> GenerationResult:
        raise TypeError("generate_spoken() got an unexpected keyword argument 'temperature'")


class OutageGenerator:
    """Stands in for a dependency outage: the LLM provider is down."""

    async def generate(
        self, prompt: object, dispatcher: object = None, context: object = None
    ) -> GenerationResult:
        raise LLMUnavailable("all models failed for tier 'simple'")

    async def generate_spoken(
        self, prompt: object, dispatcher: object, context: object, speak: object, **_kw: object
    ) -> GenerationResult:
        raise LLMUnavailable("all models failed for tier 'simple'")


class ExplodingSTT:
    """A dependency failure raised from inside the STT adapter."""

    name = "exploding-stt"

    async def transcribe_stream(
        self,
        frames: AsyncIterator[bytes],
        vocab: list[str] | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[str]:
        async for _ in frames:
            pass
        raise ConnectionError("STT provider unreachable")
        yield  # pragma: no cover — makes this an async generator


def _session(generator: object, stt: object) -> tuple[VoiceSession, TraceEmitter]:
    trace = TraceEmitter(SESSION)
    session = VoiceSession(
        user_id=USER,
        session_id=SESSION,
        vad=ScriptedVAD(_SPEECH_THEN_SILENCE),
        config=PipelineConfig(),
        stt=stt,  # type: ignore[arg-type]
        endpointer=SemanticEndpointer(short_pause_ms=48, long_pause_ms=160),
        assembler=FakeAssembler(),  # type: ignore[arg-type]
        generator=generator,  # type: ignore[arg-type]
        tts=FakeTTS(),
        working=WorkingMemory(),
        trace=trace,
        greet_on_open=False,
    )
    return session, trace


async def test_programming_error_is_not_swallowed_and_is_traced() -> None:
    """A TypeError from the turn engine must escape the conversation, not vanish."""
    session, trace = _session(BuggyGenerator(), FakeSTT("hi"))

    with pytest.raises(TypeError, match="temperature"):
        async for _ in session.converse(_frames(_SPEECH_THEN_SILENCE)):
            pass

    trace.close()
    errors = [e async for e in trace.events() if e.stage == "error"]
    assert errors, "the bug must be recorded as a failed step in the trace"
    assert errors[0].data.get("programming_error") is True
    assert "Traceback" in errors[0].data.get("traceback", "")


async def test_dependency_outage_degrades_and_keeps_the_conversation_alive() -> None:
    """An LLM outage must NOT kill the session; the user hears an honest line."""
    session, trace = _session(OutageGenerator(), FakeSTT("hi"))

    audio = [chunk async for chunk in session.converse(_frames(_SPEECH_THEN_SILENCE))]

    trace.close()
    errors = [e async for e in trace.events() if e.stage == "error"]
    assert errors and errors[0].data.get("degraded") is True
    assert errors[0].data.get("programming_error") is False
    assert audio, "the companion must still say something honest, not go silent"


async def test_stt_outage_degrades_without_killing_the_session() -> None:
    """A failure inside an adapter (not our code) is a dependency failure."""
    session, trace = _session(BuggyGenerator(), ExplodingSTT())

    # STT explodes before generation is ever reached — the conversation survives.
    audio = [chunk async for chunk in session.converse(_frames(_SPEECH_THEN_SILENCE))]

    trace.close()
    errors = [e async for e in trace.events() if e.stage == "error"]
    assert errors and errors[0].data.get("degraded") is True
    assert audio, "a degraded turn still speaks honestly"


# ── the startup contract check (F3): a mis-wired engine can't reach a turn ──


class _GoodEngine:
    async def generate(
        self, prompt: object, dispatcher: object = None, context: object = None
    ) -> None: ...

    async def generate_spoken(
        self,
        prompt: object,
        dispatcher: object,
        context: object,
        speak: object,
        *,
        temperature: float | None = None,
        flush: object = None,
    ) -> None: ...


class _MissingTemperature:
    """Exactly the shape LangGraphOrchestrator had when the live path was dead."""

    async def generate(
        self, prompt: object, dispatcher: object = None, context: object = None
    ) -> None: ...

    async def generate_spoken(
        self, prompt: object, dispatcher: object, context: object, speak: object
    ) -> None: ...


class _NotAnOrchestrator:
    pass


def test_contract_accepts_an_engine_the_voice_edge_can_call() -> None:
    assert_orchestrator_contract(_GoodEngine())


def test_contract_rejects_the_engine_that_broke_production() -> None:
    with pytest.raises(OrchestratorContractError, match="temperature"):
        assert_orchestrator_contract(_MissingTemperature())


def test_contract_rejects_something_that_is_not_an_orchestrator() -> None:
    with pytest.raises(OrchestratorContractError, match="does not implement"):
        assert_orchestrator_contract(_NotAnOrchestrator())


def test_both_real_engines_satisfy_the_contract() -> None:
    """The two wired engines must both accept the voice edge's call (A1.5)."""
    from adapters.orchestrator.langgraph_orchestrator import LangGraphOrchestrator
    from core.reasoning.response_gen import ResponseGenerator

    for engine in (ResponseGenerator, LangGraphOrchestrator):
        assert_orchestrator_contract(engine.__new__(engine))
