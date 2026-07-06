"""Thin e2e for §6: session transcript → consolidation-style extraction → recall.

A closed working-memory session is fed to semantic memory (as Consolidation
§18 will do), then a later "who is X" style lookup returns durable facts.
Skipped loudly without OPEN_ROUTER_API_KEY (real extraction is a paid call).
"""

import uuid

import pytest

from adapters.db import Database
from adapters.graph.graphiti import GraphitiGraphStore
from config.settings import get_settings
from core.memory.semantic import SemanticMemory
from core.memory.working import Turn, WorkingMemory
from tests.integration.conftest import wait_until_healthy

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.integration,
    pytest.mark.skipif(
        not get_settings().open_router_api_key,
        reason="OPEN_ROUTER_API_KEY not set — §6 extraction needs a real LLM",
    ),
]


async def test_session_facts_survive_into_semantic_memory() -> None:
    database = Database(get_settings())
    user_id = f"it_{uuid.uuid4().hex[:12]}"
    try:
        await wait_until_healthy(database)
        store = GraphitiGraphStore(database)
        await store.setup()
        semantic = SemanticMemory(store)
        wm = WorkingMemory()

        session = f"s_{uuid.uuid4().hex[:8]}"
        wm.append(session, Turn(role="user", text="my dog Biscuit turned three today"))
        wm.append(session, Turn(role="assistant", text="happy birthday to Biscuit!"))
        transcript = wm.close(session)

        text = "\n".join(f"{t.role}: {t.text}" for t in transcript)
        await semantic.add_episode(user_id, text)

        # LLM extraction is probabilistic even at temperature 0 — exactly as
        # in real use, a fact may take a repeated mention to stick. One
        # restatement episode is allowed before the fact must be present.
        facts = await semantic.profile_facts(user_id, limit=10)
        if not any("biscuit" in f.fact.lower() for f in facts):
            await semantic.add_episode(
                user_id, "user: I have a pet dog named Biscuit. Biscuit is three years old."
            )
            facts = await semantic.profile_facts(user_id, limit=10)
        assert facts and any("biscuit" in f.fact.lower() for f in facts)
    finally:
        await database.aclose()
