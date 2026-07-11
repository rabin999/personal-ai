"""Batch A verification — drive the REAL engine (greeting + conversation), not curated
scenarios. Checks: #1 greeting time-of-day, #2 name frequency, #4 tone. Text/core path."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

GREET = (
    "[The user just opened the app to talk with you. Greet them first, warmly and CASUALLY, in "
    "ONE short natural spoken line — like a friend genuinely glad they showed up. You don't need "
    "to use their name. For THIS greeting: if you GENUINELY know their local time of day, you can "
    "nod to it naturally — but ONLY if the prompt actually tells you their local time; never guess "
    "morning/evening. Make it FRESH and clearly DIFFERENT from a stock 'welcome back'. Keep it "
    "informal. Do NOT ask 'how can I help' or end on a stock filler question.]"
)
CONV = [
    "hey, how's it going?",
    "work's been crazy busy, we just moved into a new apartment last month",
    "yeah the rent is brutal honestly",
    "can you check if that new restaurant near the mall is open tonight?",
    "my dad's been unwell, he has tests on monday",
    "anyway — i got the promotion!",
]


async def main() -> int:
    from api.composition import build_pipeline
    from config.settings import get_settings
    from tests.support.real_pipeline import RealTurns

    now = datetime.now(ZoneInfo("UTC"))
    ktm = now.astimezone(ZoneInfo("Asia/Kathmandu"))
    hour = ktm.hour
    part = (
        "night"
        if hour >= 21 or hour < 5
        else "morning"
        if hour < 12
        else "afternoon"
        if hour < 17
        else "evening"
    )
    print(f"GROUND TRUTH: UTC {now:%H:%M} | Kathmandu {ktm:%H:%M %A} → day_part='{part}'")

    p = await build_pipeline(get_settings())
    turns = RealTurns(p, "u_demo_001")
    try:
        await p.profiles.update("u_demo_001", {"locale": {"timezone": "Asia/Kathmandu"}})
        print("\n=== GREETING, tz=Asia/Kathmandu (expect ~'" + part + "' or time-neutral) ===")
        for _ in range(3):
            r = await turns.say(GREET, f"g_ktm_{uuid.uuid4().hex[:5]}")
            print(f"  {r.reply}")

        await p.profiles.update("u_demo_001", {"locale": {"timezone": ""}})
        print("\n=== GREETING, tz UNSET (must NOT state any time of day) ===")
        for _ in range(3):
            r = await turns.say(GREET, f"g_none_{uuid.uuid4().hex[:5]}")
            print(f"  {r.reply}")
        await p.profiles.update("u_demo_001", {"locale": {"timezone": "Asia/Kathmandu"}})

        print("\n=== CONVERSATION (name frequency + tone) ===")
        session = f"conv_{uuid.uuid4().hex[:5]}"
        replies: list[str] = []
        for m in CONV:
            r = await turns.say(m, session)
            replies.append(r.reply)
            print(f"  U: {m}\n  C: {r.reply}")
        with_name = sum(1 for x in replies if "nandi" in x.lower())
        words = [len(x.split()) for x in replies]
        print(f"\n  NAME in {with_name}/{len(replies)} replies (target: small minority)")
        print(f"  avg length {sum(words) / len(words):.0f} words; per-reply {words}")
    finally:
        await p.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
