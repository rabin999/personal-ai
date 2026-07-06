"""Unit tests for Semantic Memory (spec §6) — graph store and Graphiti faked."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from adapters.graph.graphiti import GraphitiGraphStore
from adapters.llm.usage import LLMUsage, LLMUsageRecorder
from core.cost import COST_COLLECTION, CostLedger
from core.memory.semantic import SemanticMemory
from ports.graph_store import Fact
from tests.fakes import FakeDocStore

PRICING = {"openai/gpt-4.1-mini": {"input_per_mtok": 0.4, "output_per_mtok": 1.6}}


# ── core module: delegation + scoping ────────────────────────────────────


class FakeGraphStore:
    def __init__(self) -> None:
        self.episodes: list[dict[str, Any]] = []
        self.searches: list[dict[str, Any]] = []
        self.facts: list[Fact] = []

    async def setup(self) -> None: ...

    async def add_episode(self, user_id: str, text: str, timestamp: str | None = None) -> None:
        self.episodes.append({"user_id": user_id, "text": text, "timestamp": timestamp})

    async def search_facts(self, user_id: str, query: str, limit: int = 10) -> list[Fact]:
        self.searches.append({"user_id": user_id, "query": query, "limit": limit})
        return self.facts


async def test_add_episode_passes_user_scope_through() -> None:
    graph = FakeGraphStore()
    await SemanticMemory(graph).add_episode(
        "u_demo_001", "my brother is Tom", "2026-07-01T00:00:00+00:00"
    )
    assert graph.episodes == [
        {
            "user_id": "u_demo_001",
            "text": "my brother is Tom",
            "timestamp": "2026-07-01T00:00:00+00:00",
        }
    ]


async def test_facts_for_queries_entities_user_scoped() -> None:
    graph = FakeGraphStore()
    graph.facts = [Fact(fact="brother is Tom", valid_from="2026-01-01T00:00:00+00:00")]

    facts = await SemanticMemory(graph).facts_for("u_demo_001", ["Tom", "brother"])

    assert graph.searches[0]["user_id"] == "u_demo_001"
    assert "Tom" in graph.searches[0]["query"]
    assert facts[0].is_current  # no valid_to → currently true


async def test_facts_for_without_entities_skips_the_store() -> None:
    graph = FakeGraphStore()
    assert await SemanticMemory(graph).facts_for("u_demo_001", []) == []
    assert graph.searches == []


async def test_profile_facts_uses_standing_profile_query() -> None:
    graph = FakeGraphStore()
    await SemanticMemory(graph).profile_facts("u_demo_001")
    assert "preferences" in graph.searches[0]["query"]


def test_fact_validity_window_semantics() -> None:
    superseded = Fact(fact="brother is Tom", valid_to="2026-06-01T00:00:00+00:00")
    current = Fact(fact="brother is Max")
    assert not superseded.is_current
    assert current.is_current


# ── adapter: Graphiti mapping, group_id scoping, cost logging ────────────


class FakeGraphiti:
    def __init__(self) -> None:
        self.episodes: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []
        self.edges: list[SimpleNamespace] = []

    async def add_episode(self, **kwargs: Any) -> None:
        self.episodes.append(kwargs)

    async def search(self, **kwargs: Any) -> list[SimpleNamespace]:
        self.search_calls.append(kwargs)
        return self.edges

    async def build_indices_and_constraints(self) -> None: ...


class FakeDb:
    def __init__(self) -> None:
        self.fake_graphiti = FakeGraphiti()
        self.llm_usage = LLMUsageRecorder()

    def graphiti(self) -> FakeGraphiti:
        return self.fake_graphiti


@pytest.fixture
def fake_db() -> FakeDb:
    return FakeDb()


async def test_adapter_writes_episode_with_group_id_scoping(fake_db: FakeDb) -> None:
    store = GraphitiGraphStore(fake_db)  # type: ignore[arg-type]
    await store.add_episode("u_demo_001", "text", "2026-07-01T12:00:00+00:00")

    episode = fake_db.fake_graphiti.episodes[0]
    assert episode["group_id"] == "u_demo_001"
    assert episode["reference_time"] == datetime(2026, 7, 1, 12, tzinfo=UTC)


async def test_adapter_searches_only_the_users_group(fake_db: FakeDb) -> None:
    store = GraphitiGraphStore(fake_db)  # type: ignore[arg-type]
    await store.search_facts("u_demo_001", "brother")
    assert fake_db.fake_graphiti.search_calls[0]["group_ids"] == ["u_demo_001"]


async def test_adapter_maps_edges_to_facts_with_validity(fake_db: FakeDb) -> None:
    fake_db.fake_graphiti.edges = [
        SimpleNamespace(
            fact="brother is Tom",
            name="HAS_BROTHER",
            valid_at=datetime(2026, 1, 1, tzinfo=UTC),
            invalid_at=datetime(2026, 6, 1, tzinfo=UTC),
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
    ]
    store = GraphitiGraphStore(fake_db)  # type: ignore[arg-type]

    (fact,) = await store.search_facts("u_demo_001", "brother")

    assert fact.fact == "brother is Tom"
    assert fact.relation == "HAS_BROTHER"
    assert fact.valid_from == "2026-01-01T00:00:00+00:00"
    assert fact.valid_to == "2026-06-01T00:00:00+00:00"
    assert not fact.is_current


async def test_adapter_logs_llm_usage_to_cost_ledger(fake_db: FakeDb) -> None:
    docs = FakeDocStore()
    ledger = CostLedger(docs)
    store = GraphitiGraphStore(fake_db, ledger=ledger, pricing=PRICING)  # type: ignore[arg-type]

    fake_db.llm_usage._events.append(  # as if Graphiti made one LLM call
        LLMUsage(model="openai/gpt-4.1-mini", input_tokens=1_000_000, output_tokens=500_000)
    )
    await store.add_episode("u_demo_001", "text")
    await ledger.flush()

    (row,) = await docs.find(COST_COLLECTION)
    assert row["user_id"] == "u_demo_001"
    assert row["component"] == "llm"
    assert row["cost_usd"] == pytest.approx(0.4 + 0.8)
    assert row["metadata"]["task_id"] == "semantic.add_episode"


# ── usage recorder ───────────────────────────────────────────────────────


async def test_usage_recorder_parses_chat_completion_responses() -> None:
    recorder = LLMUsageRecorder()
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(
        200,
        request=request,
        json={
            "model": "openai/gpt-4.1-mini",
            "usage": {"prompt_tokens": 120, "completion_tokens": 40},
        },
    )
    await recorder.on_response(response)

    (usage,) = recorder.drain()
    assert usage.model == "openai/gpt-4.1-mini"
    assert usage.input_tokens == 120 and usage.output_tokens == 40
    assert recorder.drain() == []  # drained


async def test_usage_recorder_ignores_non_llm_and_malformed_responses() -> None:
    recorder = LLMUsageRecorder()
    other = httpx.Response(
        200, request=httpx.Request("GET", "https://openrouter.ai/api/v1/models"), json={}
    )
    broken = httpx.Response(
        200,
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        content=b"not json",
    )
    await recorder.on_response(other)
    await recorder.on_response(broken)
    assert recorder.drain() == []
