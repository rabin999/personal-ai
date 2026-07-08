"""Audio Input Pipeline (spec §19): mic → AEC → NS → AGC → Silero VAD → gate.

The VAD gate is the cost gate: while no speech is detected, nothing paid
(STT/LLM/TTS) runs downstream — idle is nearly free. Stages are individually
toggleable for WER A/B testing; the AEC↔barge-in dependency is validated;
the VAD threshold is always clamped to the profile's [vad_min, vad_max].

In production the AEC/NS/AGC DSP itself is provided by the Pipecat/WebRTC
transport — this module owns the stage configuration handed to the
transport, the gating state machine, and bounded on-demand ambient capture.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Literal, Protocol

from pydantic import BaseModel

from core.profile import AudioPrefs

GateEvent = Literal["speech_start", "speech_end"]

STAGES = ("aec", "noise_suppress", "agc")

# Frames of consecutive evidence before flipping state: quick to open
# (don't clip speech onsets), slower to close (don't chop trailing words).
START_FRAMES = 3
STOP_FRAMES = 25


class VADModel(Protocol):
    """Minimal surface of pipecat's SileroVADAnalyzer used here."""

    def voice_confidence(self, buffer: bytes) -> float: ...


class PipelineConfig(BaseModel):
    aec: bool = True
    noise_suppress: bool = True
    agc: bool = True
    vad_threshold: float = 0.6
    vad_min: float = 0.4
    vad_max: float = 0.8
    # Barge-in (§24) uses a LOWER detection bar than the turn-start gate. While the
    # companion is speaking, AEC has removed our own TTS from the mic, so the only
    # thing that raises the near-end signal is the user — and browser AEC's
    # double-talk suppression *attenuates* that near-end speech, so it often sits
    # below the normal turn-start threshold and the interrupt never fires (the
    # reported "it doesn't stop when I speak"). Detecting barge-in this much below
    # the gate threshold catches that attenuated speech; the sustained-frames guard
    # (START/_BARGE_IN_FRAMES) still rejects brief residual-echo blips.
    barge_in_sensitivity: float = 0.2

    @classmethod
    def from_prefs(cls, prefs: AudioPrefs) -> "PipelineConfig":
        return cls(
            aec=prefs.aec,
            noise_suppress=prefs.noise_suppress,
            agc=prefs.agc,
            vad_threshold=prefs.vad_threshold,
            vad_min=prefs.vad_min,
            vad_max=prefs.vad_max,
            barge_in_sensitivity=prefs.barge_in_sensitivity,
        )

    @property
    def clamped_threshold(self) -> float:
        """Rule 4: user-tunable but never outside [vad_min, vad_max]."""
        return min(self.vad_max, max(self.vad_min, self.vad_threshold))

    @property
    def barge_in_threshold(self) -> float:
        """Detection bar for speech *during playback* — lower than the turn-start
        gate (see ``barge_in_sensitivity``), floored at ``vad_min`` so it never goes
        below the profile's minimum and starts self-interrupting on noise."""
        return max(self.vad_min, self.clamped_threshold - self.barge_in_sensitivity)

    def enabled_stages(self) -> list[str]:
        return [stage for stage in STAGES if getattr(self, stage)]


def validate_audio_config(config: PipelineConfig, *, barge_in_enabled: bool) -> list[str]:
    """Rule 3: barge-in without AEC means transcribing our own TTS."""
    warnings: list[str] = []
    if barge_in_enabled and not config.aec:
        warnings.append(
            "barge-in is enabled but AEC is off: the companion will hear its "
            "own TTS and self-interrupt; enable aec or disable barge-in"
        )
    return warnings


class VADGate:
    """Speech-state machine over per-frame voice confidence (the cost gate)."""

    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        self.active = False
        self._over = 0
        self._under = 0

    def update(self, confidence: float) -> GateEvent | None:
        if confidence >= self.threshold:
            self._over += 1
            self._under = 0
            if not self.active and self._over >= START_FRAMES:
                self.active = True
                return "speech_start"
        else:
            self._under += 1
            self._over = 0
            if self.active and self._under >= STOP_FRAMES:
                self.active = False
                return "speech_end"
        return None


class AudioFrame(BaseModel):
    pcm: bytes
    speech_active: bool  # hysteretic gate state (idle-is-free paid-path gate)
    is_speech: bool = False  # raw this-frame verdict (endpointing silence timing)
    event: GateEvent | None = None
    confidence: float = 0.0  # raw VAD score (barge-in uses a lower bar than is_speech)


class AudioInputPipeline:
    def __init__(self, config: PipelineConfig, vad: VADModel) -> None:
        self._config = config
        self._vad = vad
        self._gate = VADGate(config.clamped_threshold)
        self._ambient_frames_left = 0

    @property
    def config(self) -> PipelineConfig:
        return self._config

    def set_stage(self, stage: str, enabled: bool) -> None:
        """Toggle aec / noise_suppress / agc independently (rule 2)."""
        if stage not in STAGES:
            raise ValueError(f"unknown audio stage '{stage}'")
        self._config = self._config.model_copy(update={stage: enabled})

    def request_ambient_window(self, frames: int) -> None:
        """Rule 5: bounded gate bypass on explicit user request — never continuous."""
        self._ambient_frames_left = max(0, frames)

    async def stream(
        self,
        frames: AsyncIterator[bytes],
        on_paid_path: Callable[[AudioFrame], Awaitable[None]] | None = None,
    ) -> AsyncIterator[AudioFrame]:
        """Yield gated frames; ``on_paid_path`` fires ONLY while speech is
        active (or inside a bounded ambient window) — the idle-is-free gate."""
        async for pcm in frames:
            confidence = self._vad.voice_confidence(pcm)
            is_speech = confidence >= self._gate.threshold
            event = self._gate.update(confidence)
            ambient = self._ambient_frames_left > 0
            if ambient:
                self._ambient_frames_left -= 1
            frame = AudioFrame(
                pcm=pcm,
                speech_active=self._gate.active,
                is_speech=is_speech,
                event=event,
                confidence=confidence,
            )
            if (self._gate.active or ambient) and on_paid_path is not None:
                await on_paid_path(frame)
            yield frame
