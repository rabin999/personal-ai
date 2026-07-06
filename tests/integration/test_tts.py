"""Integration tests for §23 TTS against the real OpenRouter audio endpoint.

Skipped loudly without OPEN_ROUTER_API_KEY (CI without secrets). Verifies the
real streamed audio-out call yields PCM chunks and that character cost lands
in the Cost Ledger (rule 5). Barge-in interruptibility (closing the stream
mid-chunk) is covered structurally by the §24 unit/e2e tests; audible tag
delivery (`[whisper]` etc.) is a human-listen concern (§7).
"""

import os

import pytest

from adapters.tts.grok import OpenRouterTTS
from config.settings import Settings
from core.cost import COST_COLLECTION, CostLedger
from tests.fakes import FakeDocStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("OPEN_ROUTER_API_KEY"),
        reason="OPEN_ROUTER_API_KEY not set — §23 needs the real audio endpoint",
    ),
]


async def test_real_synthesis_streams_audio_and_logs_character_cost() -> None:
    docs = FakeDocStore()
    ledger = CostLedger(docs)
    tts = OpenRouterTTS(Settings(), ledger=ledger)

    text = "Hey, [warm] good to hear from you. <pause> Take your time."
    audio = bytearray()
    async for chunk in tts.speak(text, user_id="it_tts_user", session_id="it_sess"):
        audio += chunk

    assert len(audio) > 0  # real PCM16 audio streamed back

    await ledger.flush()
    entries = await docs.find(COST_COLLECTION, {"user_id": "it_tts_user"})
    assert len(entries) == 1
    assert entries[0]["component"] == "tts"
    assert entries[0]["units"]["characters"] == len(text)
