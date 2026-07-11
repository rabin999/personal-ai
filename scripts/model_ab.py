"""A/B candidate reply models on the real conversation (with enforcement on = product-realistic).
Reports tone, avg length, wall-time/turn, and whether the model rejects no-reasoning calls."""

import asyncio
import time
import uuid

from api.composition import build_pipeline
from config.settings import get_settings
from tests.support.real_pipeline import RealTurns

CONV = [
    "hey, how's it going?",
    "work's crazy busy, we just moved into a new apartment",
    "my dad's been unwell, tests on monday",
    "anyway — i got the promotion!",
]
MODELS = ["z-ai/glm-5.2", "openai/gpt-5-mini", "anthropic/claude-haiku-4.5"]


async def main():
    p = await build_pipeline(get_settings())
    turns = RealTurns(p, "u_demo_001")
    try:
        await p.profiles.update("u_demo_001", {"locale": {"timezone": "Asia/Kathmandu"}})
        for model in MODELS:
            p.llm._tiers["simple"] = [model, "google/gemini-2.5-flash"]
            p.llm._tiers["moderate"] = [model, "google/gemini-2.5-flash"]
            print(f"\n===== {model} =====")
            session, words, t0, err = f"m_{uuid.uuid4().hex[:5]}", [], time.perf_counter(), False
            for m in CONV:
                try:
                    r = await turns.say(m, session)
                    words.append(len(r.reply.split()))
                    print(f"  C: {r.reply}  [{len(r.reply.split())}w]")
                except Exception as e:
                    err = True
                    print(f"  ERROR: {str(e)[:90]}")
            dt = (time.perf_counter() - t0) / max(len(CONV), 1)
            print(f"  -> avg {sum(words) / max(len(words), 1):.0f}w, {dt:.1f}s/turn, errored={err}")
    finally:
        await p.aclose()


asyncio.run(main())
