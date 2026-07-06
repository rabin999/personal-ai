"""Integration tests for §22 SER against the real emotion2vec microservice.

The model needs a GPU, so the service runs separately (design doc §17.3).
These are skipped loudly unless ``SER_SERVICE_URL`` points at a running
instance (``uv run --extra ser uvicorn services.ser_service.app:app``).
"""

import os

import pytest

from adapters.ser.emotion2vec_client import Emotion2VecSER
from config.settings import Settings
from ports.ser import EmotionRead

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("SER_SERVICE_URL"),
        reason="SER_SERVICE_URL not set — §22 needs the emotion2vec GPU service",
    ),
]


def _adapter() -> Emotion2VecSER:
    return Emotion2VecSER(Settings())  # picks up SER_SERVICE_URL from env


def _silence(seconds: float = 1.0) -> bytes:
    return b"\x00" * int(16_000 * 2 * seconds)  # PCM16 mono 16kHz


async def test_real_service_returns_a_valid_read() -> None:
    read = await _adapter().analyze(_silence(), user_id="it_ser_user")
    assert isinstance(read, EmotionRead)
    assert -1.0 <= read.valence <= 1.0 and -1.0 <= read.arousal <= 1.0
    assert 0.0 <= read.confidence <= 1.0 and read.label
