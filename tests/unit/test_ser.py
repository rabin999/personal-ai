"""Unit tests for §22 SER: emotion2vec client, VA mapping, lagging provider."""

import asyncio

import httpx
import pytest

from adapters.ser.emotion2vec_client import Emotion2VecSER
from config.settings import Settings
from ports.ser import EmotionRead
from services.ser_service.app import scores_to_read
from voice.emotion import LaggingEmotionProvider


def _adapter(url: str = "http://ser.local") -> Emotion2VecSER:
    return Emotion2VecSER(Settings(_env_file=None, ser_service_url=url, ser_timeout_s=1.0))


def _mock_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:  # type: ignore[no-untyped-def]
    """Route the adapter's self-built AsyncClient through a MockTransport."""
    real = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("adapters.ser.emotion2vec_client.httpx.AsyncClient", factory)


# ── adapter: parse, low-valence, validation fallback, disabled ────────────


async def test_adapter_parses_a_valid_read(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/analyze"
        return httpx.Response(
            200, json={"valence": 0.8, "arousal": 0.6, "label": "happy", "confidence": 0.91}
        )

    _mock_transport(monkeypatch, handler)
    read = await _adapter().analyze(b"\x00" * 320, user_id="u_demo_001")
    assert read is not None and read.label == "happy" and read.valence == 0.8


# Acceptance: an audibly low/tired utterance yields low valence/arousal.
async def test_adapter_surfaces_low_valence_arousal(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"valence": -0.7, "arousal": -0.5, "label": "sad", "confidence": 0.7}
        )

    _mock_transport(monkeypatch, handler)
    read = await _adapter().analyze(b"\x00" * 320, user_id="u_demo_001")
    assert read is not None and read.valence < 0 and read.arousal < 0


async def test_adapter_retries_once_then_falls_back_to_neutral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"valence": 9.9, "label": "broken"})  # invalid

    _mock_transport(monkeypatch, handler)
    read = await _adapter().analyze(b"\x00" * 320, user_id="u_demo_001")
    assert calls == 2  # invariant 5: validate → retry once → safe fallback
    assert read is not None and read.label == "neutral" and read.confidence == 0.0


async def test_adapter_falls_back_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    _mock_transport(monkeypatch, handler)
    read = await _adapter().analyze(b"\x00" * 320, user_id="u_demo_001")
    assert read is not None and read.label == "neutral"


async def test_adapter_disabled_when_url_unset() -> None:
    read = await _adapter(url="").analyze(b"\x00" * 320, user_id="u_demo_001")
    assert read is None  # SER off → acoustic emotion deferred (design §17.3)


# ── service: score → valence/arousal mapping (pure, no GPU) ────────────────


def test_scores_to_read_picks_top_label_and_maps_va() -> None:
    read = scores_to_read(["angry", "happy", "neutral"], [0.1, 0.75, 0.15])
    assert read.label == "happy" and read.valence > 0 and read.confidence == 0.75


def test_scores_to_read_normalizes_bilingual_label() -> None:
    read = scores_to_read(["生气/angry"], [0.9])
    assert read.label == "angry" and read.valence < 0


def test_scores_to_read_empty_is_neutral() -> None:
    read = scores_to_read([], [])
    assert read.label == "neutral" and read.confidence == 0.0


# ── lagging provider: one turn behind, never blocks (rule 2) ───────────────


class SlowSER:
    """Fake SER whose analysis completes only when released."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.read = EmotionRead(valence=-0.6, arousal=-0.4, label="tired", confidence=0.6)

    async def analyze(
        self, audio_window: bytes, *, user_id: str, session_id: str | None = None
    ) -> EmotionRead | None:
        await self.gate.wait()
        return self.read


# Acceptance: SER lagging one turn does not delay the live response.
async def test_current_never_blocks_on_in_flight_analysis() -> None:
    ser = SlowSER()
    provider = LaggingEmotionProvider(ser)

    provider.schedule(b"\x00" * 320, user_id="u_demo_001")  # turn 1 analysis starts
    assert provider.current() is None  # turn 1's response uses no acoustic read yet

    ser.gate.set()
    await asyncio.sleep(0)  # let turn 1 analysis finish in the background
    assert provider.current() == ser.read  # available for turn 2 (one turn behind)
    await provider.aclose()


async def test_schedule_rolls_in_previous_before_starting_next() -> None:
    ser = SlowSER()
    provider = LaggingEmotionProvider(ser)
    provider.schedule(b"a", user_id="u_demo_001")
    ser.gate.set()
    await asyncio.sleep(0)

    ser.gate.clear()  # next analysis will hang
    provider.schedule(b"b", user_id="u_demo_001")
    assert provider.current() == ser.read  # previous read still served while new one runs
    await provider.aclose()


async def test_aclose_cancels_in_flight() -> None:
    ser = SlowSER()  # never released
    provider = LaggingEmotionProvider(ser)
    provider.schedule(b"a", user_id="u_demo_001")
    await provider.aclose()
    assert provider.current() is None
