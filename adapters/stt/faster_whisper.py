"""Adapter: local faster-whisper STT (implements ports.stt.STT, spec §20).

The spec allows faster-whisper local as the STT engine; OpenRouter exposes
no transcription endpoint (verified against its live catalog), so local it
is — $0 per utterance, still ledger-logged with second units. Streaming is
windowed: accumulated audio is re-decoded every window and emitted as a
partial; the final decode carries per-word confidence. Vocabulary boosting
seeds the decoder prompt with the user's names/terms (§6).
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from functools import cached_property
from typing import Any

import numpy as np

from core.cost import CostEntry, CostLedger, CostMetadata
from ports.stt import TranscriptPiece, WordConfidence

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
_BYTES_PER_SECOND = SAMPLE_RATE * 2  # PCM16 mono

# Re-decode cadence for partials: small enough to feed endpointing, large
# enough to keep CPU load sane.
PARTIAL_WINDOW_S = 1.5


class FasterWhisperSTT:
    def __init__(
        self,
        model_size: str = "base",
        ledger: CostLedger | None = None,
        partial_window_s: float = PARTIAL_WINDOW_S,
    ) -> None:
        self._model_size = model_size
        self._ledger = ledger
        self._partial_window_s = partial_window_s

    @cached_property
    def _model(self) -> Any:
        from faster_whisper import (  # type: ignore[import-untyped]  # no py.typed
            WhisperModel,
        )

        return WhisperModel(self._model_size, device="cpu", compute_type="int8")

    async def transcribe_stream(
        self,
        frames: AsyncIterator[bytes],
        vocab: list[str] | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[TranscriptPiece]:
        prompt = ", ".join(vocab) if vocab else None
        buffer = bytearray()
        next_partial_at = int(self._partial_window_s * _BYTES_PER_SECOND)

        async for frame in frames:
            buffer.extend(frame)
            if len(buffer) >= next_partial_at:
                text = await self._decode_text(bytes(buffer), prompt)
                next_partial_at += int(self._partial_window_s * _BYTES_PER_SECOND)
                if text:
                    yield TranscriptPiece(text=text, is_final=False)

        if not buffer:
            return
        final = await self._decode_final(bytes(buffer), prompt)
        self._log_cost(user_id, session_id, seconds=len(buffer) / _BYTES_PER_SECOND)
        yield final

    async def _decode_text(self, audio: bytes, prompt: str | None) -> str:
        segments, _ = await asyncio.to_thread(
            self._model.transcribe,
            _to_float32(audio),
            initial_prompt=prompt,
            beam_size=1,  # fast draft for partials
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def _decode_final(self, audio: bytes, prompt: str | None) -> TranscriptPiece:
        segments, _ = await asyncio.to_thread(
            self._model.transcribe,
            _to_float32(audio),
            initial_prompt=prompt,
            word_timestamps=True,  # per-word confidence (rule 3)
            beam_size=5,
        )
        words: list[WordConfidence] = []
        texts: list[str] = []
        for segment in segments:
            texts.append(segment.text.strip())
            for word in segment.words or []:
                words.append(
                    WordConfidence(
                        word=word.word.strip(),
                        confidence=max(0.0, min(1.0, float(word.probability))),
                    )
                )
        return TranscriptPiece(text=" ".join(texts).strip(), words=words, is_final=True)

    def _log_cost(self, user_id: str, session_id: str | None, seconds: float) -> None:
        if self._ledger is None:
            return
        self._ledger.log(
            CostEntry(
                user_id=user_id,
                component="stt",
                provider="faster-whisper-local",
                units={"seconds": round(seconds, 2)},
                cost_usd=0.0,  # local inference
                metadata=CostMetadata(session_id=session_id),
            )
        )


def _to_float32(pcm16: bytes) -> "np.ndarray[Any, Any]":
    return np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
