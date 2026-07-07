"""Real-call memory cleanup + conflict supersession (plan Item 4) — real stores.

Two acceptance scenarios against REAL Qdrant + Neo4j/Graphiti:
- episodic dedup collapses near-duplicate events to one canonical entry;
- a changed semantic fact supersedes the old one (validity window closed), the
  new value is current, and history is preserved (nothing deleted).
"""

import uuid

import pytest

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]


async def test_episodic_dedup_collapses_near_duplicates(real_turns) -> None:
    user = f"u_dedup_{uuid.uuid4().hex[:8]}"  # fresh user → isolated
    ep = real_turns.episodic
    # The same event, three ways (the SYPNL pollution shape) + one distinct event.
    await ep.write(user, "s", ["user bought 10 shares of SYPNL at 230"])
    await ep.write(user, "s", ["bought 10 shares of SYPNL at $230"])
    await ep.write(user, "s", ["bought 10 shares of SYPNL at 230"])
    await ep.write(user, "s", ["went hiking in the hills"])

    before = await ep.list_recent(user, limit=50)
    assert len(before) == 4

    removed = await ep.deduplicate(user)
    after = await ep.list_recent(user, limit=50)

    assert removed == 2, f"expected 2 duplicates removed, got {removed}"
    assert len(after) == 2, f"expected 2 entries after dedup, got {len(after)}"
    texts = " | ".join(e.text.lower() for e in after)
    assert texts.count("sypnl") == 1, "the SYPNL trade should survive exactly once"
    assert "hiking" in texts, "the distinct event must be preserved"

    # Idempotent: a second pass removes nothing.
    assert await ep.deduplicate(user) == 0


async def test_changed_fact_supersedes_the_old_value(real_turns) -> None:
    user = f"u_sup_{uuid.uuid4().hex[:8]}"
    sem = real_turns.semantic
    await sem.add_episode(user, "My name is Priya and I live in Kathmandu.")
    await sem.add_episode(user, "I moved out of Kathmandu; I now live in Pokhara.")

    facts = await sem._graph.search_facts(user, "where does Priya live city home", limit=15)
    kathmandu = [f for f in facts if "kathmandu" in f.fact.lower()]
    pokhara = [f for f in facts if "pokhara" in f.fact.lower()]

    assert pokhara, f"new value not recalled — facts: {[f.fact for f in facts]}"
    # New value is current (open validity window).
    assert any(f.valid_to is None for f in pokhara), "Pokhara should be the current fact"
    # Old value superseded, not deleted (history preserved with a closed window).
    assert kathmandu, "old fact was deleted instead of superseded (history lost)"
    assert any(f.valid_to is not None for f in kathmandu), "Kathmandu should be superseded"
