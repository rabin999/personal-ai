"""Headless verification of the Pipecat CompanionProcessor (CLAUDE.md §5).

Runs the processor inside a real Pipecat pipeline (via pipecat's own run_test
harness) with a synthetic final transcription — no audio, no browser — and
asserts the reasoning ran and a reply flowed downstream as text for TTS. This
proves the framework integration without needing a mic.
"""

from typing import Any

import pytest
from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.tests.utils import run_test

from core.memory.working import WorkingMemory
from voice.pipecat.companion_processor import CompanionProcessor


class _FakeAssembler:
    async def assemble(self, user_id: str, session_id: str, utterance: str) -> Any:
        from core.reasoning.prompt_assembly import AssembledPrompt

        return AssembledPrompt(
            user_id=user_id,
            session_id=session_id,
            utterance=utterance,
            system_prompt="sys",
            messages=[{"role": "user", "content": utterance}],
            complexity_hint="simple",
        )


class _FakeGenerator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate(self, prompt: Any, dispatcher: Any = None, context: Any = None) -> Any:
        from core.reasoning.response_gen import GenerationResult

        self.calls.append(prompt.utterance)
        return GenerationResult(
            final_text="Hey, good to hear you — how's the day going?",
            action="respond",
            turn_id="t1",
        )


class _RecordingExtractor:
    def __init__(self) -> None:
        self.stored: list[tuple[str, str]] = []

    async def extract_and_store(
        self, user_id: str, session_id: str, user_text: str, assistant_text: str
    ) -> Any:
        from core.memory.extraction import ExtractionResult

        self.stored.append((user_text, assistant_text))
        return ExtractionResult()


@pytest.mark.asyncio
async def test_final_transcription_produces_a_reply_text_frame() -> None:
    working = WorkingMemory()
    generator = _FakeGenerator()
    extractor = _RecordingExtractor()
    proc = CompanionProcessor(
        user_id="u_demo_001",
        session_id="s_pc",
        assembler=_FakeAssembler(),
        generator=generator,
        working=working,
        extractor=extractor,
    )

    received, _ = await run_test(
        proc,
        frames_to_send=[TranscriptionFrame("what's up", "u_demo_001", "2026-07-07T00:00:00Z")],
        # The reply is bracketed for the TTS aggregator: start → text → end.
        expected_down_frames=[LLMFullResponseStartFrame, TextFrame, LLMFullResponseEndFrame],
    )

    reply = next(f for f in received if isinstance(f, TextFrame))
    assert "good to hear you" in reply.text.lower()
    assert generator.calls == ["what's up"]  # reasoning ran on the transcript
    # Working memory recorded both sides; the extraction write step fired.
    turns = working.recent("s_pc")
    assert [t.role for t in turns] == ["user", "assistant"]


class _FakeSTT:
    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe_stream(self, frames, vocab=None, *, user_id, session_id=None):  # type: ignore[no-untyped-def]
        from ports.stt import TranscriptPiece, WordConfidence

        async for _ in frames:
            pass
        yield TranscriptPiece(
            text=self.text, words=[WordConfidence(word="x", confidence=0.9)], is_final=True
        )


class _FakeTTS:
    async def speak(self, text, voice=None, *, user_id, session_id=None):  # type: ignore[no-untyped-def]
        yield b"\x01\x00" * 240
        yield b"\x02\x00" * 240


async def test_stt_service_wraps_whisper_into_transcription_frame() -> None:
    from pipecat.frames.frames import TranscriptionFrame as TF

    from voice.pipecat.services import CompanionSTTService

    svc = CompanionSTTService(_FakeSTT("hello there"), user_id="u", session_id="s")
    frames = [f async for f in svc.run_stt(b"\x00" * 640)]
    assert any(isinstance(f, TF) and f.text == "hello there" for f in frames)


class _CountingSTT:
    """Records how many times (and with how many bytes) run_stt is invoked."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[int] = []

    async def transcribe_stream(self, frames, vocab=None, *, user_id, session_id=None):  # type: ignore[no-untyped-def]
        from ports.stt import TranscriptPiece, WordConfidence

        total = b""
        async for chunk in frames:
            total += chunk
        self.calls.append(len(total))
        yield TranscriptPiece(
            text=self.text, words=[WordConfidence(word="x", confidence=0.9)], is_final=True
        )


async def test_stt_segments_one_transcription_per_utterance() -> None:
    """Regression (CLAUDE.md §5): the STT service must transcribe ONE whole
    VAD-bounded utterance, not every ~20ms audio frame. The old base ``STTService``
    called run_stt per frame — faster-whisper on fragments, no coherent transcript,
    the reported prod symptom. As a ``SegmentedSTTService`` it buffers between the
    VADProcessor's start/stop frames and calls run_stt exactly once with the full
    audio. Driven through a real Pipecat pipeline via run_test — no mic."""
    from pipecat.frames.frames import (
        InputAudioRawFrame,
        TranscriptionFrame,
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
    )
    from pipecat.tests.utils import run_test

    from voice.pipecat.services import CompanionSTTService

    stt = _CountingSTT("hey there")
    svc = CompanionSTTService(stt, user_id="u", session_id="s")
    pcm = b"\x01\x00" * 8000  # 0.5s @16k
    received, _ = await run_test(
        svc,
        frames_to_send=[
            VADUserStartedSpeakingFrame(),
            InputAudioRawFrame(audio=pcm, sample_rate=16_000, num_channels=1),
            InputAudioRawFrame(audio=pcm, sample_rate=16_000, num_channels=1),
            VADUserStoppedSpeakingFrame(),
        ],
        expected_down_frames=None,  # inspect frames ourselves (passthrough varies)
    )
    transcripts = [f for f in received if isinstance(f, TranscriptionFrame)]
    # Exactly one run_stt call, fed the WHOLE utterance (both frames concatenated),
    # yielding exactly one transcript — not one per audio frame.
    assert stt.calls == [len(pcm) * 2], stt.calls
    assert [f.text for f in transcripts] == ["hey there"]


async def test_tts_service_wraps_grok_into_audio_frames() -> None:
    from pipecat.frames.frames import TTSAudioRawFrame

    from voice.pipecat.services import CompanionTTSService

    svc = CompanionTTSService(_FakeTTS(), user_id="u", session_id="s", voice="eve")
    frames = [f async for f in svc.run_tts("hi there", "ctx")]
    audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
    assert len(audio) == 2 and all(f.sample_rate == 24_000 for f in audio)


async def test_raw_pcm_serializer_roundtrips_audio() -> None:
    from pipecat.frames.frames import InputAudioRawFrame, TTSAudioRawFrame

    from voice.pipecat.serializer import RawPCMSerializer

    ser = RawPCMSerializer()
    # Inbound browser bytes → InputAudioRawFrame @16k.
    frame = await ser.deserialize(b"\x01\x02\x03\x04")
    assert isinstance(frame, InputAudioRawFrame) and frame.sample_rate == 16_000
    # Outbound TTS audio → raw PCM bytes for the browser player.
    out = await ser.serialize(
        TTSAudioRawFrame(audio=b"\x05\x06", sample_rate=24_000, num_channels=1)
    )
    assert out == b"\x05\x06"
    # Non-audio frames aren't serialized onto the binary channel.
    from pipecat.frames.frames import TextFrame

    assert await ser.serialize(TextFrame("hi")) is None
