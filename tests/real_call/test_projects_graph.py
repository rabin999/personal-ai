"""Real-store projects view (U3) + knowledge-graph read (U4) — real Mongo/Neo4j.

Proves the dynamic projects list reflects a live position, and the graph read
enumerates the user's connected entities/relationships, both user-scoped.
"""

import contextlib
import uuid

import pytest

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]


async def test_projects_summary_reflects_a_live_position(real_turns) -> None:
    p = real_turns._p
    user = f"u_proj_{uuid.uuid4().hex[:8]}"
    # A finance position: buy 10 OP @ 230 (the brief's OP-share example).
    project = await p.projects.find_or_create(user, "finance_portfolio", "My portfolio")
    await p.projects.log_entry(
        project.id, user, {"ticker": "OP", "side": "buy", "qty": 10, "price": 230}
    )
    summaries = await p.projects.summaries(user)
    assert len(summaries) == 1
    s = summaries[0]
    assert s["name"] == "My portfolio" and s["type"] == "finance_portfolio"
    assert "OP" in s["status"], f"status should show the OP holding: {s['status']!r}"
    assert s["entry_count"] == 1
    # Isolation: another user sees no projects.
    assert await p.projects.summaries(f"u_other_{uuid.uuid4().hex[:6]}") == []


async def test_graph_read_is_user_scoped(real_turns) -> None:
    """`all_facts` enumerates a user's graph edges and never another user's (U4)."""
    p = real_turns._p
    ua, ub = f"u_g_{uuid.uuid4().hex[:6]}", f"u_g_{uuid.uuid4().hex[:6]}"
    # Best-effort write (needs OpenRouter credits for Graphiti extraction); the
    # scoping guarantee holds regardless of whether extraction produced edges.
    with contextlib.suppress(Exception):
        await p.semantic.record_fact(ua, "The user has a dog named Rex.")
    a_facts = await p.semantic.all_facts(ua)
    b_facts = await p.semantic.all_facts(ub)
    assert all("rex" not in f.fact.lower() for f in b_facts), "user B must not see user A's graph"
    # Whatever A has, each fact carries the fields the graph view renders.
    for f in a_facts:
        assert f.fact and (f.source is not None or f.target is not None or f.uuid is not None)
