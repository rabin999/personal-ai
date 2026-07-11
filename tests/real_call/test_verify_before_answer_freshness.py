"""Real-drive proof of the verify-before-answer invariant (docs/RETRIEVAL_POLICY.md).

The user's original complaint: "who is the PM of Nepal?" answered from stale training data
(Prachanda) instead of searching. This drives the REAL engine (real model + real stores) and
asserts a volatile officeholder question SEARCHES first and never ships a stale/refusal draft.
Prints the actual answer so the deployed behaviour is inspectable, not assumed."""

import uuid

import pytest

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]

# Predecessors that would indicate a stale, unsearched parametric answer.
_STALE_MARKERS = ("prachanda", "dahal", "sher bahadur", "deuba", "oli")
_REFUSAL_MARKERS = ("can't access", "cannot access", "don't have real-time", "as an ai i can't")


async def test_current_pm_of_nepal_searches_and_is_not_stale(real_turns) -> None:
    session = f"fresh_{uuid.uuid4().hex[:6]}"
    result = await real_turns.say("who is the current prime minister of Nepal?", session)

    print(f"\n[verify-before-answer] Q: who is the current PM of Nepal?\n  A: {result.reply}")
    print(f"  searched: {result.searches}\n  self-reflected: {result.reflected}")

    assert result.reply.strip(), "empty reply — the user heard nothing"
    # It MUST have gone to the web for this volatile officeholder fact (bucket B).
    assert result.searches, f"answered without searching a volatile fact: {result.reply}"
    low = result.reply.lower()
    assert not any(m in low for m in _REFUSAL_MARKERS), f"false refusal shipped: {result.reply}"
    assert not any(m in low for m in _STALE_MARKERS), (
        f"shipped a stale predecessor as the answer: {result.reply}"
    )
