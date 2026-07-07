"""Real-call deferred memory routing (Item 9) — real Mongo + Qdrant/Graphiti.

The live turn writes only the raw log; the background router promotes it via the
cursor exactly once (no double-write), off the conversation path.
"""

import uuid

import pytest

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]


async def test_raw_log_then_cursor_routes_once(real_turns) -> None:
    p = real_turns._p  # the live pipeline
    user = f"u_route_{uuid.uuid4().hex[:8]}"
    session = "s_route"

    # Live path: raw log only (deferred) — nothing promoted yet.
    await p.conversations.record_turn(
        user_id=user,
        session_id=session,
        turn_index=1,
        user_text="I adopted a cat named Biscuit last week.",
        assistant_text="Aww!",
    )

    # The router promotes it via the cursor, exactly once.
    n1 = await p.memory_router.route_pending()
    n2 = await p.memory_router.route_pending()
    assert n1 >= 1, "the raw turn was not routed"
    assert n2 == 0, "cursor failed — a routed turn was re-processed (double-write risk)"

    # And a poison-free turn actually promoted a durable fact.
    facts = await p.semantic._graph.search_facts(user, "cat pet Biscuit", limit=8)
    assert any("biscuit" in f.fact.lower() or "cat" in f.fact.lower() for f in facts), (
        f"nothing promoted: {[f.fact for f in facts]}"
    )
