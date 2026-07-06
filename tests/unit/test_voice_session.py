"""Unit tests for the VoiceSession runtime (design §17.1) — collaborators faked.

Drives a full turn through the assembled path and asserts the trace records
each stage start-to-finish, idle-is-free short-circuits, and audio streams
back — without any real model, LLM, or datastore.
"""

from collections.abc import AsyncIterator

from core.memory.working import WorkingMemory
from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import GenerationResult
from ports.stt import TranscriptPiece, WordConfidence
from voice.endpointing import SemanticEndpointer
from voice.pipeline import PipelineConfig
from voice.session import VoiceSession
from voice.trace import TraceEmitter

USER = "u_demo_001"
SESSION = "s_voice"


class ScriptedVAD:
    def __init__(self, confidences: list[float]) -> None:
        self.confidences = confidences
        self.index = 0

    def voice_confidence(self, buffer: bytes) -> float:
        value = self.confidences[min(self.index, len(self.confidences) - 1)]
        self.index += 1
        return value


class FakeSTT:
    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe_stream(
        self, frames: AsyncIterator[bytes], vocab: list[str] | None = None,
        *, user_id: str, session_id: str | None = None,
    ) -> AsyncIterator[TranscriptPiece]:
        async for _ in frames:
            pass
        yield TranscriptPiece(text="partial", is_final=False)
        yield TranscriptPiece(
            text=self.text,
            words=[WordConfidence(word=self.text or "x", confidence=0.9)],
            is_final=True,
        )


class FakeAssembler:
    def __init__(self, working: WorkingMemory) -> None:
        self._working = working
        self.last_emotion: object = None

    async def assemble(
        self, user_id: str, session_id: str, utterance: str,
        emotion: object = None,
    ) -> AssembledPrompt:
        self.last_emotion = emotion
        return AssembledPrompt(
            user_id=user_id, session_id=session_id, utterance=utterance,
            system_prompt="sys", messages=[{"role": "user", "content": utterance}],
            complexity_hint="simple",
        )


class FakeGenerator:
    async def generate(self, prompt: object) -> GenerationResult:
        return GenerationResult(
            final_text="Hey, good to hear you.", action="respond", turn_id="t1"
        )


class FakeTTS:
    async def speak(
        self, text_with_tags: str, voice: str | None = None,
        *, user_id: str, session_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        yield b"\x01\x02" * 100
        yield b"\x03\x04" * 100


async def _frames(confidences: list[float]) -> AsyncIterator[bytes]:
    for _ in confidences:
        yield b"\x00" * 640


def _session(vad: ScriptedVAD, stt: FakeSTT, working: WorkingMemory, trace: TraceEmitter):  # type: ignore[no-untyped-def]
    return VoiceSession(
        user_id=USER, session_id=SESSION, vad=vad, config=PipelineConfig(),
        stt=stt, endpointer=SemanticEndpointer(), assembler=FakeAssembler(working),  # type: ignore[arg-type]
        generator=FakeGenerator(), tts=FakeTTS(), working=working, trace=trace,  # type: ignore[arg-type]
    )


async def test_full_turn_traces_every_stage_and_streams_audio() -> None:
    trace = TraceEmitter(SESSION)
    working = WorkingMemory()
    # Idle, then speech, then trailing silence to close the gate.
    confidences = [0.05] * 5 + [0.9] * 20 + [0.02] * 30
    session = _session(ScriptedVAD(confidences), FakeSTT("hey there"), working, trace)

    audio = bytearray()
    async for chunk in session.run_turn(_frames(confidences)):
        audio += chunk
    trace.close()

    stages = [e.stage async for e in trace.events()]
    expected_stages = (
        "session", "vad", "stt", "endpoint",
        "assembly", "generation", "response", "tts",
    )
    for expected in expected_stages:
        assert expected in stages, f"missing trace stage: {expected}"
    assert len(audio) > 0  # TTS audio streamed back
    # The turn is now in working memory (user + assistant).
    recent = working.recent(SESSION)
    assert [t.role for t in recent] == ["user", "assistant"]
    assert recent[0].text == "hey there"


async def test_silence_short_circuits_before_any_paid_stage() -> None:
    trace = TraceEmitter(SESSION)
    working = WorkingMemory()
    session = _session(ScriptedVAD([0.02] * 30), FakeSTT("unused"), working, trace)

    audio = [chunk async for chunk in session.run_turn(_frames([0.02] * 30))]
    trace.close()

    stages = [e.stage async for e in trace.events()]
    assert "stt" not in stages and "generation" not in stages  # idle is free
    assert audio == []
    assert working.recent(SESSION) == []
