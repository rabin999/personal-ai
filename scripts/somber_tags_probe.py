"""Real-call proof for the grave-subject delivery + delivery-tags fixes.

The full audio path (Grok STT/TTS) needs the xAI key, which lives only on the prod box — so the
audio stages are substituted (a recorder stands in for TTS, the utterance is handed in as text).
The REPLY ITSELF — wording, register, inline delivery tags — comes from the REAL OpenRouter model
through the WIRED orchestrator (`pipeline.orchestrator.generate_spoken`), the same engine the
voice edge runs. The model is NOT mocked; this is the §6 "judge the actual response" bar for
these two changes. (The full VAD→STT→TTS proof is run on prod after deploy.)

Run:  PYTHONPATH=. uv run python -m scripts.somber_tags_probe
"""

from __future__ import annotations

import asyncio
import re

from api.composition import build_pipeline
from config.settings import get_settings
from core.tools.registry import ToolContext

USER = "u_demo_001"

_FLIPPANT = re.compile(
    r"\b(torched|offed|dude|bro|bruh|man|lol|haha|whatever|no biggie)\b", re.IGNORECASE
)
_TAG = re.compile(r"\[[^\[\]]+\]|<[^<>]+>")

SCENARIOS = [
    ("grave_news", "what's the news about the man who set himself on fire in Kathmandu?"),
    ("ordinary_chat", "I finally finished setting up my new home office today"),
    ("info_question", "explain the difference between sparse and dense retrieval"),
    # A spread of emotional registers — the delivery should adapt: excited→playful/upbeat,
    # happy→light-warm, sad→gentle/soft, frustrated→calm/steady, confused→patient & clear.
    ("excited", "I just got the job I've been dreaming about for years, I can't believe it!!"),
    ("happy", "had such a lovely, relaxed weekend with my family, feeling really good"),
    ("sad", "I've been feeling really down and lonely this week, everything feels heavy"),
    ("frustrated", "ugh I've been staring at this same bug for hours and nothing works"),
    ("confused", "honestly I'm so confused about how all this retrieval stuff even fits together"),
]


class _Recorder:
    def __init__(self) -> None:
        self.chunks: list[str] = []

    async def speak(self, text: str) -> None:
        if text.strip():
            self.chunks.append(text.strip())

    async def flush(self) -> None:
        pass


async def main() -> None:
    settings = get_settings()
    pipeline = await build_pipeline(settings)
    for label, utterance in SCENARIOS:
        sid = f"probe_{label}"
        prompt = await pipeline.assembler.assemble(USER, sid, utterance)
        rec = _Recorder()
        ctx = ToolContext(user_id=USER, session_id=sid, project_id=None)
        result = await pipeline.orchestrator.generate_spoken(
            prompt, pipeline.dispatcher, ctx, rec.speak, flush=rec.flush
        )
        spoken = " ".join(rec.chunks) or (result.voice_text or result.final_text)
        tags = _TAG.findall(spoken)
        flippant = _FLIPPANT.findall(spoken)
        print(f"\n===== {label} =====")
        print(f"  utterance : {utterance}")
        print(f"  spoken    : {spoken}")
        print(f"  tags      : {tags or '(none)'}")
        print(f"  flippant  : {flippant or '(none)'}")


if __name__ == "__main__":
    asyncio.run(main())
