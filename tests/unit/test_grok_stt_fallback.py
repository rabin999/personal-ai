"""Grok STT must never silently drop an utterance: when the remote call fails (the real prod
incident was `httpx.ReadTimeout` from the box), it transcribes the SAME audio with the local
fallback instead of returning nothing. Without this, a timed-out transcription reads to the user
as "it didn't hear me" plus a long wait."""

from collections.abc import AsyncIterator

import pytest

from adapters.stt.grok import GrokSTT
from config.settings import Settings
from ports.stt import TranscriptPiece


async def _frames(*chunks: bytes) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


class _FakeWhisper:
    """Stands in for FasterWhisperSTT — records that it was asked, returns a known transcript."""

    def __init__(self, text: str = "why do people keep fighting") -> None:
        self.text = text
        self.calls = 0
        self.preloaded = False
        self.seen_pcm = b""

    def preload(self) -> None:
        self.preloaded = True

    async def transcribe_stream(
        self,
        frames: AsyncIterator[bytes],
        vocab: list[str] | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[TranscriptPiece]:
        self.calls += 1
        async for f in frames:
            self.seen_pcm += f
        if self.text:
            yield TranscriptPiece(text=self.text, words=[], is_final=True)


def _settings() -> Settings:
    return Settings(stt_engine="grok", stt_timeout_s=8.0)


async def _collect(stt: GrokSTT, pcm: bytes) -> list[str]:
    return [p.text async for p in stt.transcribe_stream(_frames(pcm), user_id="u1")]


async def test_falls_back_to_whisper_when_grok_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A remote failure (_transcribe returns None) recovers the utterance via the fallback."""
    fake = _FakeWhisper("why do people keep fighting")
    stt = GrokSTT(_settings(), fallback=fake)

    async def _fail(self, pcm, vocab, user_id, session_id):
        return None  # mirrors _transcribe's own catch on httpx.ReadTimeout

    monkeypatch.setattr(GrokSTT, "_transcribe", _fail)
    pcm = b"\x01\x02" * 8000
    out = await _collect(stt, pcm)
    assert out == ["why do people keep fighting"]  # recovered, not dropped
    assert fake.calls == 1  # the fallback actually ran
    assert fake.seen_pcm == pcm  # ...on the SAME audio


async def test_dropped_when_grok_fails_and_no_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    stt = GrokSTT(_settings(), fallback=None)

    async def _fail(self, pcm, vocab, user_id, session_id):
        return None

    monkeypatch.setattr(GrokSTT, "_transcribe", _fail)
    assert await _collect(stt, b"\x01\x02" * 8000) == []  # nothing to fall back to


async def test_no_fallback_when_grok_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeWhisper("should not be used")
    stt = GrokSTT(_settings(), fallback=fake)

    async def _ok(self, pcm, vocab, user_id, session_id):
        return ("balendra shah is the pm", [], 1.0)

    monkeypatch.setattr(GrokSTT, "_transcribe", _ok)
    out = await _collect(stt, b"\x01\x02" * 8000)
    assert out == ["balendra shah is the pm"]
    assert fake.calls == 0  # Grok worked → fallback untouched


async def test_transcribe_returns_none_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real _transcribe must CONVERT an httpx failure into None (the fallback signal),
    never raise — otherwise the turn would crash instead of degrading."""
    import httpx

    stt = GrokSTT(_settings(), fallback=None)

    class _Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def post(self, *a: object, **k: object):
            raise httpx.ReadTimeout("xAI hung")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Boom())
    result = await stt._transcribe(b"\x01\x02" * 100, None, "u1", None)
    assert result is None


async def test_empty_audio_yields_nothing_and_skips_both() -> None:
    fake = _FakeWhisper()
    stt = GrokSTT(_settings(), fallback=fake)
    out = [p.text async for p in stt.transcribe_stream(_frames(), user_id="u1")]
    assert out == []
    assert fake.calls == 0


def test_preload_warms_the_fallback() -> None:
    fake = _FakeWhisper()
    GrokSTT(_settings(), fallback=fake).preload()
    assert fake.preloaded  # the fallback is warm so the first Grok failure isn't a cold load
