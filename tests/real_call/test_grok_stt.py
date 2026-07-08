"""Real-call Grok STT adapter (#18) — round-trips real audio through the live xAI API.

Synthesizes a phrase with Grok TTS at 16 kHz, feeds the PCM to the Grok STT adapter,
and asserts the transcript comes back. Proves the swappable STT engine works end to
end against the real endpoint (faster-whisper stays the default; this is the opt-in).
"""

import httpx
import pytest

from adapters.stt.grok import GrokSTT
from config.settings import get_settings

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]


async def _tts_16k(phrase: str) -> bytes:
    s = get_settings()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{s.xai_base_url}/tts",
            headers={"Authorization": f"Bearer {s.xai_api_key}"},
            json={
                "text": phrase,
                "language": "en",
                "voice_id": "orion",
                "output_format": {"codec": "pcm", "sample_rate": 16000},
            },
        )
        r.raise_for_status()
        return r.content


async def test_grok_stt_transcribes_a_real_utterance() -> None:
    settings = get_settings()
    if not settings.xai_api_key:
        pytest.skip("xAI key not set")
    phrase = "The quick brown fox jumps over the lazy dog."
    pcm = await _tts_16k(phrase)
    assert len(pcm) > 1000, "TTS produced no audio"

    stt = GrokSTT(settings)

    async def frames():
        for i in range(0, len(pcm), 3200):
            yield pcm[i : i + 3200]

    pieces = [
        p async for p in stt.transcribe_stream(frames(), vocab=["NEPSE"], user_id="u_demo_001")
    ]
    assert pieces, "Grok STT returned no transcript"
    assert pieces[-1].is_final
    text = pieces[-1].text.lower()
    # Not a strict WER check — just that it genuinely transcribed the utterance.
    assert "quick brown fox" in text or "lazy dog" in text, text
