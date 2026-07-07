"""Pipecat STT/TTS services wrapping our adapters (CLAUDE.md §5).

Thin bridges so the SAME faster-whisper (§20) and Grok TTS (§23) adapters the
native runtime uses plug into a Pipecat pipeline: the STT service turns a buffered
utterance into a TranscriptionFrame; the TTS service turns the companion's reply
text into streamed PCM audio frames. Pipecat owns buffering (VAD-bounded audio to
STT) and text aggregation (reply text to TTS).
"""

from collections.abc import AsyncGenerator

from pipecat.frames.frames import Frame, TranscriptionFrame, TTSAudioRawFrame
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601

from ports.stt import STT
from ports.tts import TTS

STT_SAMPLE_RATE = 16_000
TTS_SAMPLE_RATE = 24_000  # Grok gpt-audio pcm16 output (§23)


class CompanionSTTService(STTService):
    """faster-whisper (§20) as a Pipecat STT service."""

    def __init__(self, stt: STT, *, user_id: str, session_id: str) -> None:
        super().__init__(sample_rate=STT_SAMPLE_RATE)
        self._stt = stt
        self._user_id = user_id
        self._session_id = session_id

    async def run_stt(  # type: ignore[override]  # pipecat annotates gen as coroutine
        self, audio: bytes
    ) -> AsyncGenerator[Frame | None, None]:
        async def _one() -> AsyncGenerator[bytes, None]:
            yield audio

        text = ""
        async for piece in self._stt.transcribe_stream(
            _one(), None, user_id=self._user_id, session_id=self._session_id
        ):
            if piece.is_final:
                text = piece.text
        if text.strip():
            yield TranscriptionFrame(text, self._user_id, time_now_iso8601())


class CompanionTTSService(TTSService):
    """Grok Voice TTS (§23) as a Pipecat TTS service."""

    def __init__(
        self, tts: TTS, *, user_id: str, session_id: str, voice: str | None = None
    ) -> None:
        super().__init__(sample_rate=TTS_SAMPLE_RATE)
        self._tts = tts
        self._user_id = user_id
        self._session_id = session_id
        self._voice = voice

    async def run_tts(  # type: ignore[override]  # pipecat annotates gen as coroutine
        self, text: str, context_id: str
    ) -> AsyncGenerator[Frame | None, None]:
        async for chunk in self._tts.speak(
            text, self._voice, user_id=self._user_id, session_id=self._session_id
        ):
            yield TTSAudioRawFrame(audio=chunk, sample_rate=TTS_SAMPLE_RATE, num_channels=1)
