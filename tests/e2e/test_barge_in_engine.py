"""Engine-level end-to-end for barge-in (spec §24) through the REAL VoiceSession.

Barge-in is a runtime control-flow property — "when the user speaks while the
companion is talking, playback stops, the in-flight turn is cancelled, queued
audio is dropped, and a fresh turn starts with context intact." That property is
independent of *which* model wrote the reply, so we drive the real
``VoiceSession._consume`` state machine with controllable STT / TTS / generator
collaborators (deterministic timing) rather than the live LLM. The browser
mic + acoustic-echo-cancellation path still needs a human with a real mic — see
docs/TEST_REPORT.md for the exact manual step.

What this proves against the real state machine:
- full-duplex: mic frames are processed WHILE a reply streams;
- sustained fresh speech (>= _BARGE_IN_FRAMES) triggers an interrupt;
- the in-flight generation task is actually cancelled (CancelledError reaches it);
- queued TTS audio is drained (the caller stops receiving the old reply);
- a new turn runs for the interrupting utterance;
- working memory keeps the prior turns (context intact across the interrupt).
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from core.memory.working import WorkingMemory
from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import GenerationResult
from ports.stt import TranscriptPiece
from voice.endpointing import SemanticEndpointer
from voice.pipeline import PipelineConfig
from voice.session import VoiceSession
from voice.trace import TraceEmitter, TraceEvent


class RecordingTrace(TraceEmitter):
    """Keeps every emitted event so the test can assert on the pipeline after
    the conversation (the real emitter fans out to the WS consumer and drops)."""

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


# 640-byte PCM16 frame = 20ms at 16kHz. Speech frames are non-zero, silence is
# zero — the fake VAD keys off that so the whole timeline is deterministic.
SPEECH = b"\x02\x00" * 320
SILENCE = b"\x00" * 640


class ScriptedVAD:
    """voice_confidence(): loud for non-silent frames, silent for zeros."""

    def voice_confidence(self, buffer: bytes) -> float:
        return 0.9 if any(buffer) else 0.0


class ScriptedSTT:
    """Returns the next queued transcript per finished utterance."""

    def __init__(self, transcripts: list[str]) -> None:
        self._transcripts = transcripts
        self.calls = 0

    async def transcribe_stream(
        self,
        frames: AsyncIterator[bytes],
        vocab: list[str] | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[TranscriptPiece]:
        async for _ in frames:  # drain the frame stream like a real STT
            pass
        text = self._transcripts[min(self.calls, len(self._transcripts) - 1)]
        self.calls += 1
        yield TranscriptPiece(text=text, is_final=True)


class SlowTTS:
    """Streams many small chunks with a gap between them, so a reply is 'playing'
    long enough to be interrupted mid-stream (and cancellation reaches it)."""

    def __init__(self, chunks: int = 25, gap_s: float = 0.02) -> None:
        self._chunks = chunks
        self._gap_s = gap_s
        self.cancelled = False

    async def speak(
        self,
        text_with_tags: str,
        voice: str | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        try:
            for _ in range(self._chunks):
                yield b"\x11\x22" * 240  # 480-byte PCM16 chunk
                await asyncio.sleep(self._gap_s)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class ScriptedAssembler:
    async def assemble(
        self,
        user_id: str,
        session_id: str,
        utterance: str,
        *,
        emotion=None,
        sound=None,
        health=None,
    ) -> AssembledPrompt:
        return AssembledPrompt(
            user_id=user_id,
            session_id=session_id,
            utterance=utterance,
            system_prompt="you are a companion",
            messages=[{"role": "user", "content": utterance}],
            complexity_hint="simple",
        )


class ScriptedGenerator:
    """generate_spoken streams the whole reply through ``speak`` (so it can be
    cancelled mid-flight) and records how many turns ran + whether it was
    interrupted."""

    def __init__(self) -> None:
        self.turns_started = 0
        self.turns_completed = 0
        self.interrupted = 0

    async def generate_spoken(
        self,
        prompt,
        dispatcher,
        context,
        speak: Callable[[str], Awaitable[None]],
    ) -> GenerationResult:
        self.turns_started += 1
        reply = f"a calm, warm reply about: {prompt.utterance}"
        try:
            await speak(reply)
        except asyncio.CancelledError:
            self.interrupted += 1
            raise
        self.turns_completed += 1
        return GenerationResult(final_text=reply, voice_text=reply, action="respond", turn_id="t")


async def _script(frames: list[tuple[bytes, float]]) -> AsyncIterator[bytes]:
    """Yield (frame, delay-after) so the turn task has real time to start
    streaming before the interrupting frames arrive."""
    for pcm, delay in frames:
        yield pcm
        if delay:
            await asyncio.sleep(delay)


def _session(stt: ScriptedSTT, tts: SlowTTS, gen: ScriptedGenerator):
    working = WorkingMemory()
    return (
        VoiceSession(
            user_id="u_test_bargein",
            session_id="s_bargein",
            vad=ScriptedVAD(),
            config=PipelineConfig(),
            stt=stt,  # type: ignore[arg-type]
            endpointer=SemanticEndpointer(short_pause_ms=100, long_pause_ms=400),
            assembler=ScriptedAssembler(),  # type: ignore[arg-type]
            generator=gen,  # type: ignore[arg-type]
            tts=tts,  # type: ignore[arg-type]
            working=working,
            trace=RecordingTrace("s_bargein"),
            barge_in=True,
        ),
        working,
    )


@pytest.mark.asyncio
async def test_user_speech_mid_reply_interrupts_and_starts_a_new_turn() -> None:
    stt = ScriptedSTT(["tell me about the ocean.", "wait, what's the weather in tokyo?"])
    tts = SlowTTS(chunks=25, gap_s=0.02)  # ~0.5s reply — plenty to interrupt
    gen = ScriptedGenerator()
    session, working = _session(stt, tts, gen)

    frames: list[tuple[bytes, float]] = []
    # 1) first utterance: 6 speech frames (opens the gate) ...
    frames += [(SPEECH, 0.0)] * 6
    # ... then silence past the 100ms short-pause threshold → endpoint → turn 1.
    frames += [(SILENCE, 0.0)] * 8
    # give turn 1 time to start streaming audio before we interrupt
    frames += [(SILENCE, 0.05)]
    # 2) BARGE-IN: sustained fresh speech over the playing reply (>= 8 frames)
    frames += [(SPEECH, 0.005)] * 12
    # 3) trailing silence → endpoint the interrupting utterance → turn 2
    frames += [(SILENCE, 0.01)] * 10
    # let turn 2 finish
    frames += [(SILENCE, 0.05)] * 4

    chunks = [c async for c in session.converse(_script(frames))]

    trace = session._trace  # type: ignore[attr-defined]
    stages = [e.stage for e in trace.recorded]

    # A barge-in was detected and recorded in the trace.
    assert "barge_in" in stages, f"no barge-in in trace stages: {stages}"
    # The in-flight generation actually received the cancellation.
    assert gen.interrupted >= 1, "generation was not cancelled on barge-in"
    assert tts.cancelled, "TTS stream was not closed on barge-in"
    # Two turns ran: the interrupted one + the new one for the interrupting speech.
    assert gen.turns_started >= 2, f"expected a second turn, got {gen.turns_started}"
    # The interrupted reply did not play in full: two uninterrupted replies would
    # yield 2*25 = 50 chunks; a truncated first reply + full second yields fewer.
    assert len(chunks) < 50, f"first reply not truncated on barge-in: {len(chunks)} chunks"
    # Context intact: working memory still holds the first user turn.
    texts = [t.text for t in working.recent("s_bargein", n=20)]
    assert any("ocean" in t for t in texts), f"lost prior-turn context: {texts}"
    assert any("tokyo" in t.lower() for t in texts), f"new utterance missing: {texts}"


class FastTTS:
    """Streams a lot of audio INSTANTLY (no gaps) → the turn task finishes almost
    immediately, but the audio represents many seconds of playback. Models the real
    case: the server sends the whole reply fast while the client keeps playing it."""

    def __init__(self, chunks: int = 120) -> None:
        self._chunks = chunks
        self.cancelled = False

    async def speak(
        self,
        text_with_tags: str,
        voice: str | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        try:
            for _ in range(self._chunks):
                # 4800-byte 24kHz PCM16 chunk = 0.1s of audio each → 120 = 12s.
                yield b"\x11\x22" * 2400
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.mark.asyncio
async def test_barge_in_fires_during_trailing_playback_after_turn_finished() -> None:
    """C2 core fix: the server sends the whole reply in a few ms, but the client
    keeps PLAYING it for seconds. Interrupting during that trailing playback — after
    the turn task is already done — must still stop it. Before the fix, barge-in was
    armed only while the turn task ran, so interrupting 'in the middle' of what you
    HEAR did nothing and the voice kept talking."""
    stt = ScriptedSTT(["tell me a long story.", "stop stop stop"])
    tts = FastTTS(chunks=120)  # ~12s of audio, streamed instantly
    gen = ScriptedGenerator()
    session, _ = _session(stt, tts, gen)

    frames: list[tuple[bytes, float]] = []
    frames += [(SPEECH, 0.0)] * 6 + [(SILENCE, 0.0)] * 8  # utterance → endpoint → turn 1
    # let the turn STREAM ALL audio and FINISH (it's instant), so turn.done() is True
    frames += [(SILENCE, 0.1)]
    # now interrupt during the trailing playback window (turn task already done)
    frames += [(SPEECH, 0.005)] * 12
    frames += [(SILENCE, 0.01)] * 10 + [(SILENCE, 0.05)] * 4

    _ = [c async for c in session.converse(_script(frames))]
    stages = [e.stage for e in session._trace.recorded]  # type: ignore[attr-defined]

    assert "barge_in" in stages, "interrupt during trailing playback did not register"
    # a second turn ran for the interrupting utterance → it actually listened
    assert gen.turns_started >= 2, "did not start listening to the interrupting speech"


@pytest.mark.asyncio
async def test_barge_in_flushes_queued_audio_and_logs_the_stop_sequence() -> None:
    """C2: real interruption stops the OUTGOING AUDIO, not just generation. The
    trace must show the full stop sequence — interruption detected → TTS stream
    closed + queued audio flushed + generation cancelled → listening — with the
    real count of already-synthesized chunks that were dropped from the queue."""
    stt = ScriptedSTT(["tell me a very long story.", "stop, what's the time?"])
    # A fast producer with a tiny gap so chunks pile up in the out-queue → there is
    # genuinely queued synthesized audio to flush at the interrupt instant.
    tts = SlowTTS(chunks=60, gap_s=0.002)
    gen = ScriptedGenerator()
    session, _ = _session(stt, tts, gen)

    frames: list[tuple[bytes, float]] = []
    frames += [(SPEECH, 0.0)] * 6
    frames += [(SILENCE, 0.0)] * 8
    frames += [(SILENCE, 0.05)]  # let the reply start streaming audio
    frames += [(SPEECH, 0.005)] * 12  # barge-in
    frames += [(SILENCE, 0.01)] * 10
    frames += [(SILENCE, 0.05)] * 4

    _ = [c async for c in session.converse(_script(frames))]
    trace = session._trace  # type: ignore[attr-defined]
    barge = [e for e in trace.recorded if e.stage == "barge_in"]

    # Two-phase stop is recorded: detection, then the confirmed stop with a flush.
    phases = [e.data.get("phase") for e in barge]
    assert "detected" in phases, f"no detection phase: {phases}"
    assert "stopped" in phases, f"no stop-confirmation phase: {phases}"
    stopped = next(e for e in barge if e.data.get("phase") == "stopped")
    # The flush actually ran (field present + a real, non-negative count).
    assert "flushed_chunks" in stopped.data, "flush count not recorded"
    assert isinstance(stopped.data["flushed_chunks"], int)
    assert "generation cancelled" in stopped.message and "listening" in stopped.message
    # The real stop happened: generation cancelled + TTS closed.
    assert gen.interrupted >= 1 and tts.cancelled


# Near-end speech that browser AEC double-talk suppression has attenuated: still
# non-silent, but its VAD score sits BELOW the turn-start gate (0.6) — the failure
# mode behind "it doesn't stop when I speak". First sample = 1 (vs 2 for loud).
ATTENUATED = b"\x01\x00" * 320


class AttenuationVAD:
    """Loud speech → 0.9 (opens the gate); attenuated near-end speech → 0.5 (below
    the 0.6 gate but above the 0.4 barge-in bar); silence → 0.0."""

    def voice_confidence(self, buffer: bytes) -> float:
        if not any(buffer):
            return 0.0
        first = int.from_bytes(buffer[0:2], "little", signed=True)
        return 0.9 if abs(first) >= 2 else 0.5


@pytest.mark.asyncio
async def test_aec_attenuated_speech_still_interrupts() -> None:
    """The F1 fix: while the companion is speaking, AEC has removed our own TTS, so
    barge-in detects at a LOWER bar (0.4) than turn-start (0.6). Near-end speech
    attenuated to 0.5 by double-talk suppression — which the old is_speech>=0.6
    check ignored — now interrupts the reply."""
    stt = ScriptedSTT(["tell me a long story.", "actually stop"])
    tts = SlowTTS(chunks=30, gap_s=0.02)
    gen = ScriptedGenerator()
    working = WorkingMemory()
    session = VoiceSession(
        user_id="u_test_bargein",
        session_id="s_bargein",
        vad=AttenuationVAD(),  # type: ignore[arg-type]
        config=PipelineConfig(),  # gate 0.6, barge-in bar 0.4
        stt=stt,  # type: ignore[arg-type]
        endpointer=SemanticEndpointer(short_pause_ms=100, long_pause_ms=400),
        assembler=ScriptedAssembler(),  # type: ignore[arg-type]
        generator=gen,  # type: ignore[arg-type]
        tts=tts,  # type: ignore[arg-type]
        working=working,
        trace=RecordingTrace("s_bargein"),
        barge_in=True,
    )

    frames: list[tuple[bytes, float]] = []
    frames += [(SPEECH, 0.0)] * 6  # loud utterance opens the gate (0.9 ≥ 0.6)
    frames += [(SILENCE, 0.0)] * 8  # endpoint → turn 1
    frames += [(SILENCE, 0.05)]  # let the reply start playing
    # ATTENUATED interrupt: 0.5 is BELOW the 0.6 gate (old is_speech = False → no
    # barge-in) but ABOVE the 0.4 barge-in bar; sustained past _BARGE_IN_FRAMES.
    frames += [(ATTENUATED, 0.005)] * 12
    frames += [(SILENCE, 0.01)] * 10  # endpoint the interrupting utterance
    frames += [(SILENCE, 0.05)] * 4

    chunks = [c async for c in session.converse(_script(frames))]
    stages = [e.stage for e in session._trace.recorded]  # type: ignore[attr-defined]

    assert "barge_in" in stages, f"attenuated near-end speech didn't interrupt: {stages}"
    assert gen.interrupted >= 1, "generation not cancelled by attenuated barge-in"
    assert tts.cancelled, "TTS not stopped by attenuated barge-in"
    # Two uninterrupted 30-chunk replies would be 60; a truncated turn-1 + full
    # turn-2 is fewer, proving turn 1 was cut short by the attenuated interrupt.
    assert len(chunks) < 60, f"first reply not truncated: {len(chunks)} chunks"


@pytest.mark.asyncio
async def test_short_echo_blip_does_not_falsely_interrupt() -> None:
    """A brief speech blip during playback (shorter than _BARGE_IN_FRAMES, e.g. a
    residual-echo transient even with AEC on) must NOT interrupt the reply — the
    reported self-interrupt failure mode. The reply plays to completion."""
    stt = ScriptedSTT(["how are you doing today?"])
    tts = SlowTTS(chunks=30, gap_s=0.02)  # ~0.6s reply
    gen = ScriptedGenerator()
    session, _ = _session(stt, tts, gen)

    frames: list[tuple[bytes, float]] = []
    frames += [(SPEECH, 0.0)] * 6  # utterance
    frames += [(SILENCE, 0.0)] * 8  # endpoint → turn 1
    frames += [(SILENCE, 0.05)]  # let the reply start playing
    frames += [(SPEECH, 0.004)] * 4  # BLIP: only 4 frames, below the 8-frame guard
    frames += [(SILENCE, 0.03)] * 25  # let the reply finish uninterrupted

    chunks = [c async for c in session.converse(_script(frames))]
    stages = [e.stage for e in session._trace.recorded]  # type: ignore[attr-defined]

    assert "barge_in" not in stages, "a short blip falsely triggered barge-in"
    assert gen.interrupted == 0, "reply was cancelled by a sub-threshold blip"
    assert gen.turns_completed == 1, "reply did not complete"
    assert len(chunks) == 30, f"reply did not play in full: {len(chunks)} chunks"


@pytest.mark.asyncio
async def test_interrupt_then_continue_same_topic_keeps_context() -> None:
    """Interrupt, then the new utterance continues the SAME topic. The prior turn
    must still be in working memory so the companion has the thread."""
    stt = ScriptedSTT(["tell me about black holes.", "wait — but how big can they get?"])
    tts = SlowTTS(chunks=25, gap_s=0.02)
    gen = ScriptedGenerator()
    session, working = _session(stt, tts, gen)

    frames: list[tuple[bytes, float]] = []
    frames += [(SPEECH, 0.0)] * 6
    frames += [(SILENCE, 0.0)] * 8
    frames += [(SILENCE, 0.05)]
    frames += [(SPEECH, 0.005)] * 12  # interrupt
    frames += [(SILENCE, 0.01)] * 10  # endpoint the follow-up
    frames += [(SILENCE, 0.05)] * 4

    _ = [c async for c in session.converse(_script(frames))]
    stages = [e.stage for e in session._trace.recorded]  # type: ignore[attr-defined]
    texts = [t.text for t in working.recent("s_bargein", n=20)]

    assert "barge_in" in stages
    assert gen.turns_started >= 2
    assert any("black holes" in t for t in texts), f"lost the topic thread: {texts}"
    assert any("how big" in t for t in texts), f"follow-up missing: {texts}"


# ── A4: multi-utterance accumulate/merge ──────────────────────────────────


class DelayedGenerator:
    """Delays before speaking so a quick second utterance can fold in (A4)."""

    def __init__(self, delay_s: float = 0.15) -> None:
        self.delay_s = delay_s
        self.transcripts: list[str] = []

    async def generate_spoken(self, prompt, dispatcher, context, speak) -> GenerationResult:
        self.transcripts.append(prompt.utterance)
        try:
            await asyncio.sleep(self.delay_s)  # "reasoning" before any speech
        except asyncio.CancelledError:
            raise
        await speak(f"reply to: {prompt.utterance}")
        return GenerationResult(final_text="ok", voice_text="ok", action="respond", turn_id="t")


@pytest.mark.asyncio
async def test_quick_continuation_folds_into_one_turn() -> None:
    # Utterance 1, then a quick "and also…" BEFORE the reply speaks → one combined
    # turn, not two. The first (uncombined) turn is cancelled.
    stt = ScriptedSTT(["let's plan a trip.", "and also book a hotel"])
    tts = SlowTTS(chunks=6, gap_s=0.02)
    gen = DelayedGenerator(delay_s=1.0)  # turn 1 is still reasoning (not speaking)
    session, _ = _session(stt, tts, gen)  # type: ignore[arg-type]

    frames: list[tuple[bytes, float]] = []
    frames += [(SPEECH, 0.0)] * 6 + [(SILENCE, 0.0)] * 8  # utterance 1 → endpoint → turn 1
    frames += [(SILENCE, 0.02)]  # tiny gap; turn 1 is reasoning, not speaking yet
    # utterance 2 while turn 1 hasn't spoken: >= _BARGE_IN_FRAMES so it registers as
    # an addition, cancelling the not-yet-spoken turn 1.
    frames += [(SPEECH, 0.004)] * 12 + [(SILENCE, 0.0)] * 8  # → endpoint → classify
    frames += [(SILENCE, 0.05)] * 10  # let the combined turn finish

    _ = [c async for c in session.converse(_script(frames))]
    stages = [(e.stage, e.data.get("decision")) for e in session._trace.recorded]  # type: ignore[attr-defined]

    # A multi-utterance decision was made and logged.
    assert any(s == "endpoint" and d in ("accumulate", "merge") for s, d in stages), stages
    # The FINAL turn the generator ran carried BOTH utterances combined.
    assert gen.transcripts, "generator never ran"
    assert any("plan a trip" in t and "hotel" in t for t in gen.transcripts), gen.transcripts
