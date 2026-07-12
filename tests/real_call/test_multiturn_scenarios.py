"""Multi-turn conversation eval harness (design doc §6 "proof by conversation").

Data-driven: each scenario in ``multiturn_scenarios.jsonl`` is a sequence of turns driven
through the REAL engine + REAL stores in ONE session, with per-turn assertions (did it
search, is a banned phrase absent, are list items distinct, did it carry context). This
formalises the ad-hoc multi-turn checks (recall, context-carry, freshness) into one
repeatable bundle so a regression in the conversational loop surfaces immediately.

Real model + real stores; no mocking (mocking the model here proves nothing, §6)."""

import json
import re
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]

SCENARIOS = [
    json.loads(line)
    for line in (Path(__file__).parent / "multiturn_scenarios.jsonl").read_text().splitlines()
    if line.strip()
]


def _distinct_item_count(reply: str) -> int:
    """Rough count of DISTINCT list items in a reply (numbered/bulleted or sentence-split),
    for the 'top 2 news → 2 distinct' no-duplication check."""
    parts = re.split(r"(?:\n|^)\s*(?:\d+[.)]|[-*•])\s*", reply)
    if len(parts) < 2:  # not an explicit list — fall back to sentences
        parts = re.split(r"(?<=[.!?])\s+", reply)
    seen = {p.strip().lower() for p in parts if len(p.strip()) > 12}
    return len(seen)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
async def test_multiturn_scenario(scenario: dict, real_turns) -> None:
    session = f"{scenario['name']}_{uuid.uuid4().hex[:6]}"
    print(f"\n=== scenario: {scenario['name']} ===")
    for i, turn in enumerate(scenario["turns"], 1):
        result = await real_turns.say(turn["say"], session)
        reply = result.reply
        low = reply.lower()
        print(f"  T{i} user: {turn['say']}")
        print(f"  T{i} saathi: {reply}")
        print(f"  T{i} searched: {result.searches}")
        expect = turn.get("expect", {})

        assert reply.strip(), f"[{scenario['name']} T{i}] empty reply — the user heard nothing"
        if expect.get("searched"):
            assert result.searches, f"[{scenario['name']} T{i}] expected a web search, got none"
        for banned in expect.get("not_contains", []):
            assert banned.lower() not in low, (
                f"[{scenario['name']} T{i}] banned phrase '{banned}' in reply: {reply}"
            )
        if "distinct_items" in expect:
            n = _distinct_item_count(reply)
            assert n >= expect["distinct_items"], (
                f"[{scenario['name']} T{i}] expected ≥{expect['distinct_items']} distinct items, "
                f"got {n}: {reply}"
            )
