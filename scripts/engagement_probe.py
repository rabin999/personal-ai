"""Real-call proof that the companion KEEPS THE CONVERSATION GOING (reported: "almost every
reply is non-engaging, the app doesn't keep the user in the loop"; the flat "I'm great, how are
you?" -> "Doing well, thanks for asking!" dead-end).

Drives a short real conversation through the wired orchestrator (real OpenRouter model) and, for
each turn, prints the reply and whether it gives the user something to respond to (ends on a
question / reciprocates). Not mocked. TTS/STT are substituted (a recorder speak) — the reply
wording is the real model.

Run:  PYTHONPATH=. uv run python -m scripts.engagement_probe
"""

from __future__ import annotations

import asyncio

from api.composition import build_pipeline
from config.settings import get_settings
from core.memory.working import Turn
from core.tools.registry import ToolContext

USER = "u_demo_001"

TURNS = [
    "I'm great, how are you?",
    "just been busy with work lately, lots of meetings",
    "yeah I finally set up my new home office though",
    "thanks!",
]


class _Rec:
    def __init__(self) -> None:
        self.chunks: list[str] = []

    async def speak(self, text: str) -> None:
        if text.strip():
            self.chunks.append(text.strip())

    async def flush(self) -> None:
        pass


async def main() -> None:
    pipeline = await build_pipeline(get_settings())
    sid = "engage_probe"
    for utt in TURNS:
        pipeline.working.append(sid, Turn(role="user", text=utt))
        prompt = await pipeline.assembler.assemble(USER, sid, utt)
        rec = _Rec()
        ctx = ToolContext(user_id=USER, session_id=sid, project_id=None)
        result = await pipeline.orchestrator.generate_spoken(
            prompt, pipeline.dispatcher, ctx, rec.speak, flush=rec.flush
        )
        reply = " ".join(rec.chunks) or (result.voice_text or result.final_text)
        pipeline.working.append(sid, Turn(role="assistant", text=reply))
        hook = "?" in reply
        print(f"\n  you   : {utt}")
        print(f"  saathi: {reply}")
        print(f"  keeps-going: {'YES' if hook else 'NO (dead-end)'}")


if __name__ == "__main__":
    asyncio.run(main())
