"""Unit tests for the continuous VoiceSession runtime (design §17.1) — faked deps.

Drives a full conversation turn through the assembled path and asserts the
trace records each stage, idle short-circuits before any paid stage, and audio
streams back — without any real model, LLM, or datastore. Turn-taking is
server-driven (§19 VAD gate + §21 endpointing), not push-to-talk.
"""

import asyncio
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
        self.calls = 0

    async def transcribe_stream(
        self,
        frames: AsyncIterator[bytes],
        vocab: list[str] | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[TranscriptPiece]:
        self.calls += 1
        async for _ in frames:
            pass
        yield TranscriptPiece(
            text=self.text,
            words=[WordConfidence(word=self.text or "x", confidence=0.9)],
            is_final=True,
        )


class FakeAssembler:
    async def assemble(
        self,
        user_id: str,
        session_id: str,
        utterance: str,
        emotion: object = None,
        sound: object = None,
        health: object = None,
    ) -> AssembledPrompt:
        return AssembledPrompt(
            user_id=user_id,
            session_id=session_id,
            utterance=utterance,
            system_prompt="sys",
            messages=[{"role": "user", "content": utterance}],
            complexity_hint="simple",
        )


class FakeGenerator:
    async def generate(
        self, prompt: object, dispatcher: object = None, context: object = None
    ) -> GenerationResult:
        return GenerationResult(final_text="Hey, good to hear you.", action="respond", turn_id="t1")

    async def generate_spoken(
        self, prompt: object, dispatcher: object, context: object, speak: object
    ) -> GenerationResult:
        result = await self.generate(prompt, dispatcher, context)
        await speak(result.voice_text or result.final_text)  # type: ignore[operator]
        return result


class FakeTTS:
    async def speak(
        self,
        text_with_tags: str,
        voice: str | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        yield b"\x01\x02" * 100
        yield b"\x03\x04" * 100


async def _frames(confidences: list[float]) -> AsyncIterator[bytes]:
    for _ in confidences:
        yield b"\x00" * 512  # one 16ms VAD frame


def _session(vad: ScriptedVAD, stt: FakeSTT, working: WorkingMemory, trace: TraceEmitter):  # type: ignore[no-untyped-def]
    # Short pauses so the endpointer fires quickly in the test.
    return VoiceSession(
        user_id=USER,
        session_id=SESSION,
        vad=vad,
        config=PipelineConfig(),
        stt=stt,
        endpointer=SemanticEndpointer(short_pause_ms=48, long_pause_ms=160),
        assembler=FakeAssembler(),  # type: ignore[arg-type]
        generator=FakeGenerator(),  # type: ignore[arg-type]
        tts=FakeTTS(),
        working=working,
        trace=trace,
    )


async def test_full_turn_traces_every_stage_and_streams_audio() -> None:
    trace = TraceEmitter(SESSION)
    working = WorkingMemory()
    # Idle, a burst of speech, then enough trailing silence to endpoint.
    confidences = [0.05] * 5 + [0.9] * 20 + [0.02] * 40
    session = _session(ScriptedVAD(confidences), FakeSTT("hey there"), working, trace)

    audio = bytearray()
    async for chunk in session.converse(_frames(confidences)):
        audio += chunk
    trace.close()

    stages = [e.stage async for e in trace.events()]
    expected = ("session", "vad", "stt", "endpoint", "assembly", "generation", "response", "tts")
    for stage in expected:
        assert stage in stages, f"missing trace stage: {stage}"
    assert len(audio) > 0  # Grok TTS audio streamed back
    recent = working.recent(SESSION)
    assert [t.role for t in recent] == ["user", "assistant"]
    assert recent[0].text == "hey there"


async def test_silence_short_circuits_before_any_paid_stage() -> None:
    trace = TraceEmitter(SESSION)
    working = WorkingMemory()
    stt = FakeSTT("unused")
    session = _session(ScriptedVAD([0.02] * 40), stt, working, trace)

    audio = [chunk async for chunk in session.converse(_frames([0.02] * 40))]
    trace.close()

    stages = [e.stage async for e in trace.events()]
    assert "stt" not in stages and "generation" not in stages  # idle is free
    assert stt.calls == 0 and audio == []
    assert working.recent(SESSION) == []


class CountingSTT(FakeSTT):
    """Records how many audio frames actually reached transcription."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.frames_seen = 0

    async def transcribe_stream(
        self,
        frames: AsyncIterator[bytes],
        vocab: list[str] | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[TranscriptPiece]:
        self.calls += 1
        async for _ in frames:
            self.frames_seen += 1
        yield TranscriptPiece(
            text=self.text,
            words=[WordConfidence(word=self.text or "x", confidence=0.9)],
            is_final=True,
        )


async def test_preroll_recovers_onset_frames_the_gate_swallowed() -> None:
    # §19 fix: the gate only fires speech_start after START_FRAMES(3) of speech,
    # so those onset frames precede the event. The pre-roll must fold them back
    # in so the first word is transcribed instead of clipped.
    trace = TraceEmitter(SESSION)
    working = WorkingMemory()
    stt = CountingSTT("hey there")
    # 4 idle + a 20-frame speech burst + trailing silence to endpoint.
    confidences = [0.05] * 4 + [0.9] * 20 + [0.02] * 40
    session = _session(ScriptedVAD(confidences), stt, working, trace)

    async for _ in session.converse(_frames(confidences)):
        pass
    trace.close()

    events = [e async for e in trace.events()]
    capturing = next(e for e in events if e.stage == "vad" and "capturing" in e.message)
    # The gate-opening frames (>= START_FRAMES) are recovered, not lost.
    assert capturing.data["preroll_frames"] >= 3
    # More frames reach STT than just those from speech_start onward: the full
    # 20-frame burst plus the pre-gate onset are transcribed.
    assert stt.frames_seen > 20


class _Interjection:
    def __init__(self, task_id: str, line: str) -> None:
        self.task_id = task_id
        self.line = line


class RepeatingDelivery:
    """Returns the SAME finished result on every pull (queue-race simulation)."""

    def __init__(self, task_id: str, line: str) -> None:
        self._item = _Interjection(task_id, line)
        self.calls = 0

    async def deliveries_for_pause(
        self, session_id: str, user_id: str, recent_context: str
    ) -> list[_Interjection]:
        self.calls += 1
        return [self._item]

    async def deliveries_at_open(self, user_id: str, session_id: str) -> list[_Interjection]:
        return []


async def test_background_result_delivered_at_most_once() -> None:
    # §14/§5.4: even if the queue hands back the same finished task twice, the
    # session speaks it exactly once — no "same news item 2-3x" duplication.
    trace = TraceEmitter(SESSION)
    working = WorkingMemory()
    delivery = RepeatingDelivery("task-1", "Market's open till 3 today.")
    session = _session(ScriptedVAD([0.02]), FakeSTT("x"), working, trace)
    session._delivery = delivery

    out: asyncio.Queue[bytes | None] = asyncio.Queue()
    await session._deliver_pending(out)
    await session._deliver_pending(out)  # second pull returns the same task

    trace.close()
    delivered = [e async for e in trace.events() if e.data.get("delivered")]
    assert len(delivered) == 1, f"expected one delivery, got {len(delivered)}"
    assert delivery.calls == 2  # both pulls happened; only the first spoke


class InterruptibleGenerator:
    """First reply is long/in-flight (cancellable); the second answers the
    interruption. Lets us prove barge-in cancels the in-flight generation and the
    companion then responds to the NEW input (§24)."""

    def __init__(self) -> None:
        self.calls = 0
        self.first_cancelled = False

    async def generate(
        self, prompt: object, dispatcher: object = None, context: object = None
    ) -> GenerationResult:
        self.calls += 1
        if self.calls == 1:
            try:
                # A long response still "generating" when the user barges in.
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.first_cancelled = True  # barge-in cancelled the in-flight gen
                raise
            return GenerationResult(final_text="unreachable", action="respond", turn_id="t1")
        return GenerationResult(
            final_text="Got it — you jumped in.", action="respond", turn_id="t2"
        )

    async def generate_spoken(
        self, prompt: object, dispatcher: object, context: object, speak: object
    ) -> GenerationResult:
        # Mirror the runtime: streaming path resolves to generate() then speaks.
        result = await self.generate(prompt, dispatcher, context)
        await speak(result.voice_text or result.final_text)  # type: ignore[operator]
        return result


async def test_barge_in_stops_reply_cancels_generation_and_answers_new_input() -> None:
    # §24 core: while the companion is replying, sustained fresh user speech must
    # (1) fire a barge_in event, (2) CANCEL the in-flight generation (→ TTS stops),
    # and (3) capture the new utterance and answer it. Drives the REAL _consume
    # state machine + REAL pipeline is_speech logic (only the LLM/TTS are faked —
    # barge-in doesn't depend on their content).
    trace = TraceEmitter(SESSION)
    working = WorkingMemory()
    generator = InterruptibleGenerator()
    #   4 idle → 20 speech (utterance 1) → 22 silence (first 3 endpoint the
    #   utterance → turn starts; the rest let the in-flight generation reach its
    #   await) → 14 speech (BARGE-IN, ≥ _BARGE_IN_FRAMES) → 6 silence (endpoint the
    #   interrupting utterance → second turn answers it).
    confidences = [0.02] * 4 + [0.9] * 20 + [0.02] * 22 + [0.9] * 14 + [0.02] * 6
    session = VoiceSession(
        user_id=USER,
        session_id=SESSION,
        vad=ScriptedVAD(confidences),
        config=PipelineConfig(),
        stt=FakeSTT("wait, actually listen to this"),
        endpointer=SemanticEndpointer(short_pause_ms=48, long_pause_ms=160),
        assembler=FakeAssembler(),  # type: ignore[arg-type]
        generator=generator,  # type: ignore[arg-type]
        tts=FakeTTS(),
        working=working,
        trace=trace,
    )

    # Real frames arrive over the WS with ~32ms gaps, which lets the turn task run
    # between frames; the test stream must yield likewise or _consume drains every
    # frame before the turn ever reaches generate (unrealistic).
    async def _paced_frames() -> AsyncIterator[bytes]:
        for _ in confidences:
            await asyncio.sleep(0.002)
            yield b"\x00" * 512

    async for _ in session.converse(_paced_frames()):
        pass
    trace.close()

    events = [e async for e in trace.events()]
    barge = [e for e in events if e.stage == "barge_in"]
    # (1) the interruption was detected and (2) the reply was cancelled.
    assert any("interrupted" in e.message for e in barge), "no barge-in event fired"
    assert any("cancelled" in e.message for e in barge), "reply was not cancelled"
    # (2) the in-flight generation actually received cancellation (TTS never ran).
    assert generator.first_cancelled, "in-flight generation was NOT cancelled by barge-in"
    # (3) the companion answered the NEW input after interrupting.
    assert generator.calls == 2, "no second turn after the interruption"
    responses = [e for e in events if e.stage == "response"]
    assert any("jumped in" in e.message for e in responses), "did not answer the new input"


async def test_events_are_grouped_into_turns() -> None:
    trace = TraceEmitter(SESSION)
    working = WorkingMemory()
    confidences = [0.05] * 3 + [0.9] * 20 + [0.02] * 40
    session = _session(ScriptedVAD(confidences), FakeSTT("hello"), working, trace)

    async for _ in session.converse(_frames(confidences)):
        pass
    trace.close()

    events = [e async for e in trace.events()]
    # Pre-speech "session" event is turn 0; the utterance's events are turn 1.
    assert any(e.turn == 0 for e in events)
    assert any(e.turn == 1 and e.stage == "response" for e in events)
