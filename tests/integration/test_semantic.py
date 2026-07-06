"""Integration tests for Semantic Memory (spec §6) — real Neo4j + real OpenRouter LLM.

These run Graphiti's actual extraction (paid LLM calls, fractions of a cent
on the configured mini model). They are skipped loudly when
OPEN_ROUTER_API_KEY is absent (e.g. CI without secrets) — run locally with
the key set for full verification.
"""

import uuid
from collections.abc import AsyncIterator

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from adapters.graph.graphiti import GraphitiGraphStore
from config.settings import get_settings
from core.cost import CostLedger
from core.memory.semantic import SemanticMemory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not get_settings().open_router_api_key,
        reason="OPEN_ROUTER_API_KEY not set — §6 extraction needs a real LLM",
    ),
]


@pytest.fixture
def user_id() -> str:
    return f"it_{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def real_db() -> AsyncIterator[Database]:
    # Unlike the shared `db` fixture, this one needs the real API key.
    database = Database(get_settings())
    yield database
    await database.aclose()


@pytest.fixture
async def ledger(real_db: Database) -> AsyncIterator[CostLedger]:
    yield CostLedger(MongoDocStore(real_db))
    await real_db.mongo("cost_ledger").delete_many({"user_id": {"$regex": "^it_"}})


@pytest.fixture
async def memory(real_db: Database, ledger: CostLedger) -> SemanticMemory:
    pricing_doc = await MongoDocStore(real_db).get("provider_config", "llm_pricing")
    store = GraphitiGraphStore(
        real_db, ledger=ledger, pricing=(pricing_doc or {}).get("models", {})
    )
    await store.setup()
    return SemanticMemory(store)


# Acceptance: rename yields the new fact current, the old superseded, both retrievable.
async def test_superseded_fact_keeps_history_with_validity_windows(
    memory: SemanticMemory, user_id: str
) -> None:
    await memory.add_episode(user_id, "user: my brother is called Tom", "2026-01-05T10:00:00+00:00")
    await memory.add_episode(
        user_id,
        "user: my brother legally changed his name — he is now called Max, not Tom",
        "2026-07-01T10:00:00+00:00",
    )

    facts = await memory.facts_for(user_id, ["brother"], limit=10)

    assert facts, "expected extracted facts about the brother"
    texts = " | ".join(f.fact.lower() for f in facts)
    assert "max" in texts, f"new name missing from facts: {texts}"
    assert "tom" in texts, f"old fact no longer retrievable: {texts}"
    current = [f for f in facts if f.is_current and "max" in f.fact.lower()]
    superseded = [f for f in facts if not f.is_current and "tom" in f.fact.lower()]
    assert current, f"renamed fact should be current: {texts}"
    assert superseded, f"old name should be superseded (valid_to set), got: {texts}"


# Acceptance: facts_for returns relationships with validity windows.
async def test_facts_for_resolved_entity_carries_validity_window(
    memory: SemanticMemory, user_id: str
) -> None:
    await memory.add_episode(
        user_id,
        "user: my sister Maya lives in Lisbon and works as a marine biologist",
        "2026-03-10T09:00:00+00:00",
    )

    facts = await memory.facts_for(user_id, ["Maya"], limit=10)

    assert facts and any("maya" in f.fact.lower() for f in facts)
    assert all(hasattr(f, "valid_to") for f in facts)  # window always exposed


async def test_two_user_isolation_across_graphs(memory: SemanticMemory, user_id: str) -> None:
    other = f"it_{uuid.uuid4().hex[:12]}"
    await memory.add_episode(user_id, "user: my secret startup is called NIMBUS7")
    await memory.add_episode(other, "user: I love gardening on weekends")

    other_facts = await memory.facts_for(other, ["NIMBUS7", "startup"], limit=10)
    assert all("nimbus7" not in f.fact.lower() for f in other_facts)


async def test_extraction_calls_land_in_cost_ledger(
    memory: SemanticMemory, ledger: CostLedger, user_id: str
) -> None:
    await memory.add_episode(user_id, "user: I started learning the cello this month")
    await ledger.flush()

    summary = await ledger.get(user_id, component="llm")
    assert summary.count >= 1, "Graphiti extraction must produce cost entries"
