"""Integration test for §23 TTS against the real xAI Grok TTS endpoint.

Skipped loudly without the X-AI-API key (CI without secrets). Verifies the real
call streams PCM16 audio and that character cost lands in the Cost Ledger
(rule 5). Audible tag delivery (`[whisper]` etc.) is a human-listen concern (§7).
"""

import pytest

from adapters.tts.grok import GrokTTS
from config.settings import Settings
from core.cost import COST_COLLECTION, CostLedger
from tests.fakes import FakeDocStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not Settings().xai_api_key,
        reason="X-AI-API key not set — §23 needs the real xAI Grok TTS endpoint",
    ),
]


async def test_real_grok_tts_streams_audio_and_logs_character_cost() -> None:
    docs = FakeDocStore()
    ledger = CostLedger(docs)
    tts = GrokTTS(Settings(), ledger=ledger)

    text = "Hey, [warm] good to hear from you. <pause> Take your time."
    audio = bytearray()
    async for chunk in tts.speak(text, "eve", user_id="it_tts_user", session_id="it_sess"):
        audio += chunk

    assert len(audio) > 0  # real PCM16 audio streamed back

    await ledger.flush()
    entries = await docs.find(COST_COLLECTION, {"user_id": "it_tts_user"})
    assert len(entries) == 1
    assert entries[0]["component"] == "tts" and entries[0]["provider"] == "xai-grok"
    assert entries[0]["units"]["characters"] > 0
    assert entries[0]["cost_usd"] > 0


async def test_real_grok_tts_streaming_session_keeps_one_voice_across_sentences() -> None:
    """§2b/§23: feeding multiple sentences into ONE WebSocket session must produce
    continuous audio from a single synthesis — the fix for mid-reply voice drift
    (separate per-sentence REST requests have no shared voice state). Proves the
    session opens, streams PCM as text is fed, flushes on finish, and bills chars."""
    docs = FakeDocStore()
    ledger = CostLedger(docs)
    tts = GrokTTS(Settings(), ledger=ledger)

    stream = await tts.open_stream("eve", user_id="it_stream_user", session_id="it_stream_sess")
    audio = bytearray()

    async def pump() -> None:
        async for chunk in stream.audio():
            audio.extend(chunk)

    import asyncio

    p = asyncio.create_task(pump())
    await stream.feed("Hey, it's really good to hear from you today. ")
    await stream.feed("How has your whole day actually been so far?")
    await stream.finish()
    await asyncio.wait_for(p, timeout=30)
    await stream.aclose()

    assert len(audio) > 20_000  # substantial continuous PCM from one session

    await ledger.flush()
    entries = await docs.find(COST_COLLECTION, {"user_id": "it_stream_user"})
    assert len(entries) == 1
    assert entries[0]["component"] == "tts" and entries[0]["units"]["characters"] > 0
