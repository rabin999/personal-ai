"""Integration tests for §19 with the real Silero VAD (adapters.vad.silero).

Confirms the AudioInputPipeline drives the real detector both ways: silence
never opens the gate (idle is free) and real speech does. The speech sample is
synthesized with Grok TTS, so that half is skipped without the X-AI-API key.
Skipped entirely when the ``voice`` extra isn't installed.
"""

from collections.abc import AsyncIterator

import httpx
import pytest

from config.settings import Settings
from voice.pipeline import AudioFrame, AudioInputPipeline, PipelineConfig

pytestmark = pytest.mark.integration

FRAME_BYTES = 512 * 2  # Silero @16kHz window


@pytest.fixture(scope="module")
def vad():  # type: ignore[no-untyped-def]
    pytest.importorskip("pipecat.audio.vad.silero", reason="voice extra not installed")
    from adapters.vad.silero import SileroVAD

    return SileroVAD()


async def _emit(pcm: bytes) -> AsyncIterator[bytes]:
    for i in range(0, len(pcm) - FRAME_BYTES, FRAME_BYTES):
        yield pcm[i : i + FRAME_BYTES]


async def test_real_silence_never_touches_the_paid_path(vad) -> None:  # type: ignore[no-untyped-def]
    pipeline = AudioInputPipeline(PipelineConfig(), vad)
    paid: list[AudioFrame] = []

    async def on_paid(frame: AudioFrame) -> None:
        paid.append(frame)

    async for frame in pipeline.stream(_emit(b"\x00" * FRAME_BYTES * 40), on_paid_path=on_paid):
        assert not frame.speech_active

    assert paid == []  # idle is free with the real detector (§19 cost gate)


@pytest.mark.skipif(
    not Settings().xai_api_key, reason="X-AI-API key not set — needs a speech sample"
)
async def test_real_speech_opens_the_gate(vad) -> None:  # type: ignore[no-untyped-def]
    s = Settings()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{s.xai_base_url}/tts",
            headers={"Authorization": f"Bearer {s.xai_api_key}"},
            json={"text": "Hello there, this is a test of voice detection.",
                  "language": "en", "voice_id": "leo",
                  "output_format": {"codec": "pcm", "sample_rate": 16000}},
        )
        resp.raise_for_status()
    speech = resp.content + b"\x00" * (16000 * 2)  # + trailing silence to close the gate

    pipeline = AudioInputPipeline(PipelineConfig(), vad)
    events = [f.event async for f in pipeline.stream(_emit(speech)) if f.event]
    assert "speech_start" in events and "speech_end" in events
