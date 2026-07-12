"""Proof for context-aware interjections (design §10.2 delivery).

Drives the REAL LLM to show that the opening filler REACTS to what the user said, stays
fact-free (the guard rejects any candidate with a number/result), and is PERFORMED with a
delivery tag. Also regenerates a phrase pool to show regenerated lines carry tags too.

Run:  PYTHONPATH=. uv run python scripts/contextual_ack_probe.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path


def _load_env() -> None:
    for line in Path(".env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


async def main() -> None:
    _load_env()
    from adapters.llm.openrouter import OpenRouterLLM
    from config.settings import Settings
    from core.profile import ProfileService, TraitRegistry
    from core.reasoning.prompt_assembly import AssembledPrompt
    from core.reasoning.response_gen import ResponseGenerator, _is_safe_interjection
    from core.reasoning.self_model import SelfModel
    from tests.fakes import FakeDocStore, FakeVectorStore

    settings = Settings()
    llm = OpenRouterLLM(settings)
    docs = FakeDocStore()
    gen = ResponseGenerator(
        llm, SelfModel(docs, FakeVectorStore(), llm), TraitRegistry(docs, ProfileService(docs))
    )

    cases = [
        ("i just ran my first ever half marathon this morning!", "ack_interest", "neutral"),
        ("thanks so much, that really helped me out", "ack_gratitude", "neutral"),
        ("i've been feeling pretty low and isolated lately", "ack_empathy", "down"),
        ("what's the latest on the election results", "ack_lookup", "neutral"),
    ]
    print("=== context-aware interjections (real LLM, fact-guarded) ===")
    for utt, pool, reg in cases:
        prompt = AssembledPrompt(
            user_id="u_demo_001",
            session_id="s1",
            utterance=utt,
            system_prompt="You are Companion.",
            messages=[{"role": "user", "content": utt}],
            complexity_hint="simple",
        )
        line = await gen._contextual_line(prompt, pool, reg, is_lookup=(pool == "ack_lookup"))
        tagged = "tag" if ("[" in (line or "") or "<" in (line or "")) else "-"
        ok = line and _is_safe_interjection(line)
        safe = "safe" if ok else ("fallback" if not line else "")
        print(f"\nuser: {utt}\n  -> {line!r}   [{tagged} {safe}]")

    # The generator now produces PERFORMED lines: regenerate one pool and show the tags.
    print("\n=== regenerated pool carries delivery tags ===")
    from core.phrases.generator import PhraseGenerator

    gen_p = PhraseGenerator(llm, pool_size=6)
    fresh = await gen_p.regenerate_replacements("ack_backchannel", [], 4)
    for ln in fresh:
        print("  ", repr(ln))


if __name__ == "__main__":
    asyncio.run(main())
