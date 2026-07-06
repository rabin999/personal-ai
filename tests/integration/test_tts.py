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
