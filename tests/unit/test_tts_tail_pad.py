"""Streaming TTS clips the release of the FINAL word when the request ends right on the last
character (reported: replies "cut off at the very corner", not smooth to the end). A trailing
space is padded so the model renders the last word fully; it is silent itself. These tests pin
that the pad is applied on both synthesis paths (WS stream `finish` + REST `_synthesize`).
"""

import json

import pytest

from adapters.tts.grok import GrokTTSStream, _pad_tail


def test_pad_tail_appends_space_once() -> None:
    assert _pad_tail("hello there") == "hello there "
    assert _pad_tail("hello there ") == "hello there "  # idempotent, never doubles
    assert _pad_tail("wait...") == "wait... "


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        self.closed = True


async def test_finish_pads_a_trailing_space_before_text_done() -> None:
    ws = _FakeWS()
    stream = GrokTTSStream(ws, on_close=lambda _n: None)  # type: ignore[arg-type]
    await stream.feed("Doing well, thanks for asking")
    await stream.finish()
    # The tail pad delta must be sent, and BEFORE text.done, so xAI renders the last word.
    types = [m.get("type") for m in ws.sent]
    assert types == ["text.delta", "text.delta", "text.done"]
    assert ws.sent[1] == {"type": "text.delta", "delta": " "}  # the pad
    assert ws.sent[-1] == {"type": "text.done"}


async def test_finish_is_a_noop_after_close() -> None:
    ws = _FakeWS()
    stream = GrokTTSStream(ws, on_close=lambda _n: None)  # type: ignore[arg-type]
    await stream.aclose()
    await stream.finish()  # closed → must not send anything
    assert ws.sent == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
