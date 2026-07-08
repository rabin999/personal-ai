"""Port: non-speech sound classification + vocal-register read (brief U10/U11/U12).

A lightweight audio-classification stage that runs in parallel to STT (behind this
port so it's swappable — a real CNN/CRNN like the COUGHVID-trained models, or the
heuristic default). It answers three things from a captured audio window:

- **health sounds** (U10): cough / sneeze / sniffle, for a caring check-in.
- **vocal register** (U11): is the user whispering / off their baseline energy, so
  the reply can mirror it when the ``mimic_tone`` setting is on.
- **ambient field** (U12): is there non-user sound/other voices present, for the
  "surroundings" awareness mode.

It is a probabilistic signal, never a diagnosis (§22 rule 4). Acoustic accuracy
scales with the user's mic and the concrete model behind the port; the default
heuristic exists so the pipeline + decision logic work end-to-end and a trained
model can drop in as one adapter swap.
"""

from typing import Literal, Protocol

from pydantic import BaseModel, Field

HealthSound = Literal["cough", "sneeze", "sniffle"]
Register = Literal["whisper", "soft", "normal", "loud"]


class SoundRead(BaseModel):
    """One classification of a captured audio window."""

    health_sounds: list[HealthSound] = Field(default_factory=list)
    vocal_register: Register = "normal"
    ambient_voices: bool = False  # another voice present in the field (U12)
    ambient_activity: bool = False  # non-speech ambient sound present (U12)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


NEUTRAL_SOUND = SoundRead()


class SoundClassifier(Protocol):
    async def classify(
        self,
        audio_window: bytes,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> "SoundRead | None":
        """PCM16 mono 16kHz window → a sound read (or None if the stage is off)."""
        ...
