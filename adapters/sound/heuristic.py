"""Adapter: heuristic sound classifier (implements ports.sound.SoundClassifier).

The default, dependency-light classifier so the U10-U12 pipeline + decision logic
run end-to-end without a bundled ML model. It reads simple acoustic features from
the PCM window — RMS energy (register), crest factor + short broadband bursts
(cough/sneeze), zero-crossing rate (breathy whisper vs. voiced) — and returns a
``SoundRead``.

This is intentionally approximate: real cough/sneeze accuracy wants a trained CNN
(e.g. COUGHVID) and a decent mic. Because it sits behind the ``SoundClassifier``
port, swapping in that model is one wiring line — ``core/`` never changes. The
value proven here is that the STAGE fires and feeds correct signals to the caring
check-in / tone-mirror / surroundings logic; acoustic accuracy is the mic/model item.
"""

from __future__ import annotations

import numpy as np

from ports.sound import Register, SoundRead

# Energy (RMS over int16 range) thresholds for the register bands. Tuned to be
# order-of-magnitude sensible; the exact cutoffs are per-mic and would be the
# trained model's job.
_WHISPER_RMS = 250.0
_SOFT_RMS = 900.0
_LOUD_RMS = 6000.0
# A cough/sneeze is a short, high crest-factor broadband burst.
_BURST_CREST = 6.0
_BURST_MIN_RMS = 1200.0
# Whisper is low-energy but high zero-crossing (unvoiced/breathy).
_WHISPER_ZCR = 0.18


class HeuristicSoundClassifier:
    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    async def classify(
        self, audio_window: bytes, *, user_id: str, session_id: str | None = None
    ) -> SoundRead | None:
        if not self._enabled or not audio_window:
            return None
        samples = np.frombuffer(audio_window, dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            return SoundRead()
        rms = float(np.sqrt(np.mean(samples**2)))
        peak = float(np.max(np.abs(samples)))
        crest = peak / (rms + 1e-6)
        zcr = float(np.mean(np.abs(np.diff(np.sign(samples))) > 0))

        register = _register(rms, zcr)
        health = _health_sounds(rms, crest, zcr)
        # Ambient signals are best-effort here: sustained mid-energy that isn't the
        # close speaker's clear speech reads as "something is present". A trained
        # model behind the port does this properly (U12 accuracy is the mic item).
        ambient_activity = _SOFT_RMS < rms < _LOUD_RMS and crest < 4.0 and not health
        confidence = min(1.0, rms / _LOUD_RMS) if (health or register != "normal") else 0.2
        return SoundRead(
            health_sounds=health,
            vocal_register=register,
            ambient_voices=False,
            ambient_activity=ambient_activity,
            confidence=confidence,
        )


def _register(rms: float, zcr: float) -> Register:
    if rms < _WHISPER_RMS or (rms < _SOFT_RMS and zcr > _WHISPER_ZCR):
        return "whisper"
    if rms < _SOFT_RMS:
        return "soft"
    if rms > _LOUD_RMS:
        return "loud"
    return "normal"


def _health_sounds(rms: float, crest: float, zcr: float) -> list[str]:
    # A cough/sneeze: a loud, sharp, broadband transient (high crest factor).
    if rms > _BURST_MIN_RMS and crest > _BURST_CREST:
        return ["cough"]
    return []
