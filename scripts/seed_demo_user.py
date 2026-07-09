"""Seed the sample user with a realistic portfolio + memories (S3).

`u_demo_001` had exactly ONE entity ("My portfolio") and no `OP` anywhere, while the SRC1
golden scenario asks "what's the current LTP of OP?". A fixture that cannot express the
scenario makes its "pass" meaningless — and it is why Turn 4 resolved OP as a crypto token.

Idempotent: entity ids are deterministic, so re-running overwrites rather than duplicating.
Episodic writes are content-addressed by the store, so repeats are harmless.

Run:  PYTHONPATH=. uv run python -m scripts.seed_demo_user
"""

from __future__ import annotations

import asyncio

from adapters.vector.qdrant import QdrantVectorStore
from api.composition import build_pipeline
from config.settings import get_settings
from core.memory.entities import EntityResolver

USER = "u_demo_001"

# NEPSE holdings. "OP" is the one SRC1 turns on: ambiguous on the open web (it matches the
# Optimism crypto token), unambiguous in this user's own data.
# NOTE the wording. An earlier version described these as "a share on the NEPSE (Nepal
# Stock Exchange)". Entity resolution runs BM25 over the whole utterance, so the token
# "Nepal" then matched OP and SYPNL at 0.833 for "who is the current prime minister of
# Nepal?" — two close candidates — and the ambiguity guardrail hijacked the turn with
# 'Quick check — do you mean "OP" or "SYPNL"?'. Keep descriptions free of common words
# that a normal question might contain.
HOLDINGS = [
    ("op", "OP", "OP — a NEPSE ticker in the user's share portfolio."),
    ("sypnl", "SYPNL", "SYPNL — a NEPSE ticker in the user's share portfolio."),
    ("nabil", "NABIL", "NABIL — Nabil Bank, a NEPSE ticker in the user's share portfolio."),
]

EPISODES = [
    "user bought 120 shares of OP at 300 rupees yesterday on the NEPSE",
    "user holds OP, SYPNL and NABIL in their NEPSE share portfolio",
    "bought 10 shares of SYPNL at 230",
    "user bought NABIL bank shares and watches its dividend announcements",
    "user tracks the NEPSE daily and asks for the LTP (last traded price) of their holdings",
]


async def main() -> None:
    settings = get_settings()
    pipeline = await build_pipeline(settings)
    entities = EntityResolver(QdrantVectorStore(pipeline.db, settings.embedding_model))

    for entity_id, name, description in HOLDINGS:
        await entities.index(USER, "holding", entity_id, name, description)
        print(f"  entity  holding/{entity_id:6} -> {name}")

    await pipeline.episodic.write(USER, "s_seed_demo", EPISODES)
    print(f"  episodic: {len(EPISODES)} memories")

    # Prove it back out of the real store, the way the prompt assembler reads it.
    for phrase in ("OP", "SYPNL", "NABIL"):
        hits = await entities.resolve(USER, phrase)
        print(f"  resolve({phrase!r}) -> {[(h.name, round(h.score, 3)) for h in hits]}")

    print("\nseeded", USER)


if __name__ == "__main__":
    asyncio.run(main())
