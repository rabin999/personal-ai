"""Unit tests for the Audio Input Pipeline (spec §19) — VAD model faked."""

from collections.abc import AsyncIterator

import pytest

from core.profile import AudioPrefs
from voice.pipeline import (
    START_FRAMES,
    STOP_FRAMES,
    AudioFrame,
    AudioInputPipeline,
    PipelineConfig,
    VADGate,
    validate_audio_config,
)


class ScriptedVAD:
    """Returns pre-scripted voice confidences per frame."""

    def __init__(self, confidences: list[float]) -> None:
        self.confidences = confidences
        self.index = 0

    def voice_confidence(self, buffer: bytes) -> float:
        value = self.confidences[min(self.index, len(self.confidences) - 1)]
        self.index += 1
        return value


async def _frames(n: int) -> AsyncIterator[bytes]:
    for _ in range(n):
        yield b"\x00" * 320


# Acceptance: with no speech, nothing paid runs.
async def test_silence_never_touches_the_paid_path() -> None:
    pipeline = AudioInputPipeline(PipelineConfig(), ScriptedVAD([0.05] * 100))
    paid_calls: list[AudioFrame] = []

    async def paid(frame: AudioFrame) -> None:
        paid_calls.append(frame)

    async for frame in pipeline.stream(_frames(100), on_paid_path=paid):
        assert not frame.speech_active
    assert paid_calls == []  # idle is free


async def test_speech_opens_the_gate_and_silence_closes_it() -> None:
    confidences = [0.1] * 5 + [0.9] * 30 + [0.05] * (STOP_FRAMES + 5)
    pipeline = AudioInputPipeline(PipelineConfig(), ScriptedVAD(confidences))
    events = []
    paid = 0

    async def on_paid(frame: AudioFrame) -> None:
        nonlocal paid
        paid += 1

    async for frame in pipeline.stream(_frames(len(confidences)), on_paid_path=on_paid):
        if frame.event:
            events.append(frame.event)

    assert events == ["speech_start", "speech_end"]
    assert paid > 0
    # Paid path saw only speech-active frames (plus the closing hangover).
    assert paid <= 30 + STOP_FRAMES


def test_gate_needs_consecutive_evidence_to_open() -> None:
    gate = VADGate(threshold=0.6)
    assert gate.update(0.9) is None  # one noisy frame doesn't open it
    assert gate.update(0.2) is None
    for _ in range(START_FRAMES - 1):
        assert gate.update(0.9) is None
    assert gate.update(0.9) == "speech_start"


# Acceptance: VAD threshold above vad_max is clamped.
def test_vad_threshold_clamps_to_profile_range() -> None:
    config = PipelineConfig(vad_threshold=0.95, vad_min=0.4, vad_max=0.8)
    assert config.clamped_threshold == 0.8
    low = PipelineConfig(vad_threshold=0.1, vad_min=0.4, vad_max=0.8)
    assert low.clamped_threshold == 0.4


def test_config_builds_from_profile_prefs() -> None:
    prefs = AudioPrefs(vad_threshold=0.7, aec=False)
    config = PipelineConfig.from_prefs(prefs)
    assert config.vad_threshold == 0.7 and config.aec is False


# Acceptance: AEC-off + barge-in-on raises a config warning.
def test_aec_off_with_barge_in_warns() -> None:
    warnings = validate_audio_config(PipelineConfig(aec=False), barge_in_enabled=True)
    assert warnings and "self-interrupt" in warnings[0]
    assert validate_audio_config(PipelineConfig(aec=True), barge_in_enabled=True) == []
    assert validate_audio_config(PipelineConfig(aec=False), barge_in_enabled=False) == []


# Rule 2: stages independently toggleable.
def test_stages_toggle_independently() -> None:
    pipeline = AudioInputPipeline(PipelineConfig(), ScriptedVAD([0.0]))
    assert pipeline.config.enabled_stages() == ["aec", "noise_suppress", "agc"]
    pipeline.set_stage("noise_suppress", False)
    assert pipeline.config.enabled_stages() == ["aec", "agc"]
    with pytest.raises(ValueError):
        pipeline.set_stage("reverb", True)


# Rule 5: ambient capture is bounded and reverts to gated idle.
async def test_ambient_window_is_bounded_then_gate_restores() -> None:
    pipeline = AudioInputPipeline(PipelineConfig(), ScriptedVAD([0.05] * 40))
    captured = 0

    async def on_paid(frame: AudioFrame) -> None:
        nonlocal captured
        captured += 1

    pipeline.request_ambient_window(frames=10)
    async for _ in pipeline.stream(_frames(40), on_paid_path=on_paid):
        pass

    assert captured == 10  # exactly the bounded window, then free idle again
