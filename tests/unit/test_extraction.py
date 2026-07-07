"""Unit tests for the memory-extraction WRITE step (brief §1) — LLM scripted."""

import json
from pathlib import Path

from core.memory.entities import EntityResolver
from core.memory.episodic import EpisodicMemory
from core.memory.extraction import MemoryExtractor
from core.memory.semantic import SemanticMemory
from core.profile import ProfileService, TraitRegistry
from core.projects.service import ProjectService
from tests.fakes import FakeDocStore, FakeGraphStore, FakeLLM, FakeVectorStore

USER = "u_demo_001"
DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"


def _extraction(**kw: object) -> str:
    payload = {
        "episodic_events": [],
        "semantic_facts": [],
        "trades": [],
        "store_nothing": False,
    }
    payload.update(kw)
    return json.dumps(payload)


async def _build(
    responses: list[str],
) -> tuple[MemoryExtractor, FakeGraphStore, ProjectService, str]:
    docs = FakeDocStore()
    vectors = FakeVectorStore()
    graph = FakeGraphStore()
    # Seed project-type blueprints so the extractor can create the finance ledger.
    await TraitRegistry(docs, ProfileService(docs)).seed_defaults(DEFAULTS_DIR)
    projects = ProjectService(docs, EntityResolver(vectors))
    extractor = MemoryExtractor(
        FakeLLM(responses), EpisodicMemory(vectors), SemanticMemory(graph), projects
    )
    return extractor, graph, projects, USER


async def test_distills_a_routine_into_episodic_and_semantic() -> None:
    ex, graph, _, user = await _build(
        [
            _extraction(
                episodic_events=["Took blood-pressure pill at 8pm today."],
                semantic_facts=["Takes blood-pressure medication daily around 8pm."],
            )
        ]
    )
    result = await ex.extract_and_store(user, "s1", "I take my BP pill every day at 8pm", "Got it.")
    assert result.episodic_written == 1
    assert result.semantic_written == 1
    # The distilled fact reached the (Graphiti) semantic store, user-scoped.
    assert any("8pm" in e["text"] for e in graph.episodes if e["user_id"] == user)


async def test_small_talk_stores_nothing() -> None:
    ex, graph, _, user = await _build([_extraction(store_nothing=True)])
    result = await ex.extract_and_store(user, "s1", "hey", "Hey! Good to hear you.")
    assert result.episodic_written == 0 and result.semantic_written == 0
    assert [e for e in graph.episodes if e["user_id"] == user] == []


async def test_trade_is_routed_to_the_finance_ledger_exactly_once() -> None:
    ex, _, projects, user = await _build(
        [_extraction(trades=[{"ticker": "SYPNL", "side": "buy", "qty": 10, "price": 230}])]
    )
    result = await ex.extract_and_store(user, "s1", "I bought 10 SYPNL at 230", "Nice.")
    assert result.trades_written == 1
    project = await projects.find_or_create(user, "finance_portfolio", "My portfolio")
    state = await projects.state(project.id, user)
    assert state.metrics["entry_count"] == 1
    assert state.metrics["net_invested"] == 2300.0


async def test_invalid_extraction_json_stores_nothing() -> None:
    ex, graph, _, user = await _build(["not json", "still not json"])
    result = await ex.extract_and_store(user, "s1", "whatever", "ok")
    assert result.episodic_written == 0 and result.semantic_written == 0
    assert [e for e in graph.episodes if e["user_id"] == user] == []


async def test_same_trade_is_not_relogged_on_recall() -> None:
    # Audit fix: restating a past trade must NOT create a second ledger entry.
    trade = {"ticker": "SYPNL", "side": "buy", "qty": 10, "price": 230}
    ex, _, projects, user = await _build([_extraction(trades=[trade]), _extraction(trades=[trade])])
    r1 = await ex.extract_and_store(user, "s1", "I bought 10 SYPNL at 230", "Got it.")
    r2 = await ex.extract_and_store(user, "s2", "what did I buy?", "You bought 10 SYPNL at 230.")
    assert r1.trades_written == 1
    assert r2.trades_written == 0  # duplicate guard skipped the re-log
    project = await projects.find_or_create(user, "finance_portfolio", "My portfolio")
    state = await projects.state(project.id, user)
    assert state.metrics["entry_count"] == 1  # exactly one entry


class _FakePreferences:
    def __init__(self) -> None:
        self.added: list[tuple[str, list[dict[str, str]]]] = []

    async def add(self, user_id: str, messages: list[dict[str, str]]) -> None:
        self.added.append((user_id, messages))

    async def search(self, user_id: str, query: str, limit: int = 5) -> list[str]:
        return []


async def test_preference_layer_receives_the_exchange() -> None:
    from core.memory.episodic import EpisodicMemory as _E
    from core.memory.semantic import SemanticMemory as _S
    from core.projects.service import ProjectService as _P

    docs = FakeDocStore()
    vectors = FakeVectorStore()
    prefs = _FakePreferences()
    ex = MemoryExtractor(
        FakeLLM([_extraction(store_nothing=True)]),
        _E(vectors),
        _S(FakeGraphStore()),
        _P(docs, EntityResolver(vectors)),
        preferences=prefs,
    )
    await ex.extract_and_store(USER, "s1", "I love hiking", "nice!")
    assert prefs.added and prefs.added[0][0] == USER
    assert any("hiking" in m["content"] for m in prefs.added[0][1])


def test_ensure_user_subject_prefixes_bare_facts_only() -> None:
    from core.memory.semantic import ensure_user_subject

    # Bare fact → gets a subject so Graphiti can attach it.
    assert ensure_user_subject("takes meds at 8pm") == "The user takes meds at 8pm"
    # Already-subjected facts are left as-is.
    assert ensure_user_subject("The user likes hiking") == "The user likes hiking"
    assert ensure_user_subject("User works at Acme") == "User works at Acme"
    assert ensure_user_subject("They have a dog named Trishul") == "They have a dog named Trishul"
