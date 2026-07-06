"""Integration tests for §20 STT against the real faster-whisper model.

Loads a real (tiny) Whisper model and drives the adapter's threaded,
windowed streaming + cost wiring against the actual library — catching
wiring bugs the unit-test fakes hide. Word-level accuracy / vocab-boost
quality needs real speech samples and is a human-tuned concern (§7); here we
assert the streaming contract, per-word confidence shape, and $0 local-cost
ledger entry. Skipped loudly if the model can't be fetched (offline CI).
"""

from collections.abc import AsyncIterator

import pytest

from adapters.stt.faster_whisper import FasterWhisperSTT
from core.cost import COST_COLLECTION, CostLedger
from tests.fakes import FakeDocStore

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def stt_model_size() -> str:
    faster_whisper = pytest.importorskip("faster_whisper")
    try:
        faster_whisper.WhisperModel("tiny", device="cpu", compute_type="int8")
    except Exception as exc:  # network-less CI, etc.
        pytest.skip(f"faster-whisper tiny model unavailable: {exc}")
    return "tiny"


def _tone_pcm16(seconds: float = 1.5, freq: int = 220) -> bytes:
    import math

    rate = 16_000
    samples = (
        int(0.3 * math.sin(2 * math.pi * freq * n / rate) * 32767)
        for n in range(int(rate * seconds))
    )
    out = bytearray()
    for s in samples:
        out += int(s).to_bytes(2, "little", signed=True)
    return bytes(out)


async def _frames(pcm: bytes, chunk: int = 3200) -> AsyncIterator[bytes]:
    for i in range(0, len(pcm), chunk):
        yield pcm[i : i + chunk]


async def test_streams_partials_then_final_and_logs_zero_cost(stt_model_size: str) -> None:
    docs = FakeDocStore()
    ledger = CostLedger(docs)
    stt = FasterWhisperSTT(model_size=stt_model_size, ledger=ledger, partial_window_s=0.5)

    pieces = [
        p
        async for p in stt.transcribe_stream(
            _frames(_tone_pcm16()), user_id="it_stt_user", session_id="it_sess"
        )
    ]

    assert pieces, "expected at least a final transcript piece"
    final = pieces[-1]
    assert final.is_final
    assert all(0.0 <= w.confidence <= 1.0 for w in final.words)  # per-word confidence shape

    await ledger.flush()
    entries = await docs.find(COST_COLLECTION, {"user_id": "it_stt_user"})
    assert len(entries) == 1
    assert entries[0]["component"] == "stt" and entries[0]["cost_usd"] == 0.0  # local = $0
    assert entries[0]["units"]["seconds"] > 0
