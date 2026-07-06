"""Port: streaming speech-to-text (spec §20).

Emits partial transcripts while the user speaks (feeding endpointing §21)
and a final transcript with per-word confidence when the stream closes.
"""

from collections.abc import AsyncIterator
from typing import Protocol

from pydantic import BaseModel, Field


class WordConfidence(BaseModel):
    word: str
    confidence: float = Field(ge=0.0, le=1.0)


class TranscriptPiece(BaseModel):
    text: str
    words: list[WordConfidence] = Field(default_factory=list)
    is_final: bool = False


class STT(Protocol):
    def transcribe_stream(
        self,
        frames: AsyncIterator[bytes],
        vocab: list[str] | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[TranscriptPiece]:
        """PCM16 mono 16kHz frames in → partial pieces, then one final."""
        ...
