"""Voice-output quality (spec §2b): the voice is pinned for the whole session and
recorded in the trace, so a mid-session voice change is impossible and visible.

The AUDIO being clean (no distortion, no audible voice change) ultimately needs a
human ear — see docs/TEST_REPORT.md for the manual step. What is verifiable here:
- resolve_voice normalizes a request to exactly one valid voice id;
- every TTS call in a session uses that one pinned voice;
- the pinned voice is recorded on the session + tts trace spans.
"""

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from adapters.tts.grok import DEFAULT_VOICE, VOICES, resolve_voice
from core.memory.working import WorkingMemory
from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import GenerationResult
from ports.stt import TranscriptPiece
from tests.e2e.test_barge_in_engine import (
    RecordingTrace,
    ScriptedAssembler,
    ScriptedVAD,
    _script,
)
from voice.endpointing import SemanticEndpointer
from voice.pipeline import PipelineConfig
from voice.session import VoiceSession

SPEECH = b"\x02\x00" * 320
SILENCE = b"\x00" * 640


@pytest.mark.parametrize(
    "requested,expected",
    [
        ("leo", "leo"),
        ("LEO", "leo"),  # case-normalized
        (None, DEFAULT_VOICE),  # unset → default
        ("", DEFAULT_VOICE),  # empty → default
        ("nonsense", DEFAULT_VOICE),  # invalid → default (never a silent per-call surprise)
        ("sal", "sal"),
    ],
)
def test_resolve_voice_normalizes_to_one_valid_id(requested: str | None, expected: str) -> None:
    v = resolve_voice(requested)
    assert v == expected
    assert v in VOICES


class _RecordingTTS:
    """Records the voice id passed to every speak() call."""

    def __init__(self) -> None:
        self.voices: list[str | None] = []

    async def speak(
        self,
        text_with_tags: str,
        voice: str | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        self.voices.append(voice)
        yield b"\x00\x01" * 100


class _Gen:
    async def generate_spoken(
        self,
        prompt: AssembledPrompt,
        dispatcher: object,
        context: object,
        speak: Callable[[str], Awaitable[None]],
        **_kw: object,
    ) -> GenerationResult:
        # A multi-clause reply → the session may call speak once; GrokTTS would
        # sub-chunk internally, but the voice id must be identical throughout.
        await speak("First clause here. Second clause here. Third one too.")
        return GenerationResult(final_text="ok", voice_text="ok", action="respond", turn_id="t")


class _STT:
    async def transcribe_stream(
        self,
        frames: AsyncIterator[bytes],
        vocab: list[str] | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[TranscriptPiece]:
        async for _ in frames:
            pass
        yield TranscriptPiece(text="hey there.", is_final=True)


@pytest.mark.asyncio
async def test_voice_is_pinned_and_recorded_in_trace() -> None:
    tts = _RecordingTTS()
    trace = RecordingTrace("s_voice")
    pinned = resolve_voice("leo")
    session = VoiceSession(
        user_id="u_voice",
        session_id="s_voice",
        vad=ScriptedVAD(),
        config=PipelineConfig(),
        stt=_STT(),  # type: ignore[arg-type]
        endpointer=SemanticEndpointer(short_pause_ms=100, long_pause_ms=400),
        assembler=ScriptedAssembler(),  # type: ignore[arg-type]
        generator=_Gen(),  # type: ignore[arg-type]
        tts=tts,  # type: ignore[arg-type]
        working=WorkingMemory(),
        trace=trace,
        voice=pinned,
        barge_in=False,
    )

    frames = [(SPEECH, 0.0)] * 6 + [(SILENCE, 0.0)] * 8 + [(SILENCE, 0.03)] * 6
    _ = [c async for c in session.converse(_script(frames))]

    # Every synthesis used the one pinned voice — no mid-session change.
    assert tts.voices, "TTS never spoke"
    assert all(v == "leo" for v in tts.voices), f"voice changed mid-session: {tts.voices}"

    # The pinned voice is recorded on the session + tts trace spans (observability).
    session_spans = [e for e in trace.recorded if e.stage == "session"]
    tts_spans = [e for e in trace.recorded if e.stage == "tts"]
    assert session_spans and session_spans[0].data.get("voice") == "leo"
    assert any(e.data.get("voice") == "leo" for e in tts_spans)


def test_default_voice_is_a_valid_voice() -> None:
    assert DEFAULT_VOICE in VOICES
