"""FULL-PATH proof for the time fix — drives the REAL wired engine (LangGraph orchestrator →
generate_spoken, real stores + model), NOT prompt fragments. Asserts a time question is answered
from the deterministic clock with NO web search, exact to the minute, across phrasings + places.

Run:  docker compose up -d && PYTHONPATH=. uv run python scripts/time_fullpath_probe.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_env() -> None:
    for line in Path(".env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


PHRASINGS = [
    ("what time is it in Nepal right now?", "Asia/Kathmandu"),
    ("tell me the current date and time in Nepal", "Asia/Kathmandu"),
    ("what's the time in Kathmandu", "Asia/Kathmandu"),
    ("current time in Japan", "Asia/Tokyo"),
    ("what time is it in New York", "America/New_York"),
    ("time in India right now", "Asia/Kolkata"),
]


async def main() -> None:
    _load_env()
    from tests.support.real_pipeline import RealTurns

    rt = await RealTurns.build()
    now = datetime.now(UTC)
    print(f"server UTC now: {now:%a %Y-%m-%d %H:%M}\n" + "=" * 60)
    all_ok = True
    for i, (utt, tz) in enumerate(PHRASINGS):
        r = await rt.say_spoken(utt, f"s_time_{i}")
        reply = (r.spoken and " ".join(r.spoken)) or r.reply
        true = now.astimezone(ZoneInfo(tz))
        hhmm24 = true.strftime("%H:%M")
        hhmm12 = true.strftime("%-I:%M")  # e.g. 10:44 (no leading zero)
        searched = r.searches
        exact = hhmm24 in reply or hhmm12 in reply
        ok = (not searched) and exact
        all_ok = all_ok and ok
        place = tz.split("/")[-1]
        print(f"\nQ: {utt}")
        print(f"  true {place} = {true:%H:%M %A %d %b}  (tokens: {hhmm24!r}/{hhmm12!r})")
        print(f"  searched: {searched or 'NO ✓'}")
        print(f"  exact-minute in reply: {'YES ✓' if exact else 'NO ✗'}")
        print(f"  reply: {reply}")
    print("\n" + "=" * 60)
    print("ALL CLEAN + ACCURATE ✓" if all_ok else "SOME FAILED ✗ — see above")


if __name__ == "__main__":
    asyncio.run(main())
