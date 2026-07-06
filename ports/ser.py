"""Port: speech emotion recognition (emotion2vec GPU service client) (spec §22).

``analyze`` reads prosody from an utterance's audio and returns a valence /
arousal / label read. It is a probabilistic **signal, not ground truth** —
never a diagnosis (rule 4); consumers (§10, §17) treat it as such.
"""

from typing import Protocol

from pydantic import BaseModel, Field


class EmotionRead(BaseModel):
    """One acoustic emotion read (spec §22 interface shape).

    ``valence`` (unpleasant → pleasant) and ``arousal`` (calm → activated)
    are in [-1, 1]; ``label`` is the top categorical read (e.g. "tired",
    "happy", "neutral); ``confidence`` in [0, 1].
    """

    valence: float = Field(ge=-1.0, le=1.0)
    arousal: float = Field(ge=-1.0, le=1.0)
    label: str
    confidence: float = Field(ge=0.0, le=1.0)


# Safe neutral read used when the service is unreachable or returns garbage:
# a $0, no-information signal that never fabricates an emotion (invariant 5).
NEUTRAL_READ = EmotionRead(valence=0.0, arousal=0.0, label="neutral", confidence=0.0)


class SER(Protocol):
    async def analyze(
        self,
        audio_window: bytes,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> "EmotionRead | None":
        """PCM16 mono 16kHz utterance audio → an emotion read (or None if SER off)."""
        ...
