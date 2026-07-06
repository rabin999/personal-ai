"""Integration tests for §19 with the real Silero VAD (pipecat voice extra).

Confirms the AudioInputPipeline drives the actual SileroVADAnalyzer and that
the idle-is-free gate holds against real silence — no paid callbacks fire.
Skipped loudly when the ``voice`` extra isn't installed (core/CI env).
"""

from collections.abc import AsyncIterator

import pytest

from voice.pipeline import AudioFrame, AudioInputPipeline, PipelineConfig

pytestmark = pytest.mark.integration

SAMPLE_RATE = 16_000


@pytest.fixture(scope="module")
def silero():  # type: ignore[no-untyped-def]
    silero_mod = pytest.importorskip(
        "pipecat.audio.vad.silero", reason="voice extra not installed (uv sync --extra voice)"
    )
    return silero_mod.SileroVADAnalyzer(sample_rate=SAMPLE_RATE)


def _silence_frames(analyzer, n: int) -> AsyncIterator[bytes]:  # type: ignore[no-untyped-def]
    # Silero consumes a fixed window; feed exactly that many zero samples.
    window_bytes = analyzer.num_frames_required() * 2

    async def gen() -> AsyncIterator[bytes]:
        for _ in range(n):
            yield b"\x00" * window_bytes

    return gen()


async def test_real_silence_never_touches_the_paid_path(silero) -> None:  # type: ignore[no-untyped-def]
    pipeline = AudioInputPipeline(PipelineConfig(), silero)
    paid: list[AudioFrame] = []

    async def on_paid(frame: AudioFrame) -> None:
        paid.append(frame)

    async for frame in pipeline.stream(_silence_frames(silero, 20), on_paid_path=on_paid):
        assert not frame.speech_active

    assert paid == []  # idle is free with the real detector (§19 cost gate)
