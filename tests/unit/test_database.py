"""Unit tests for the Database Layer (spec §1) — drivers stubbed, no real I/O.

Driver clients construct lazily (no connection until first operation), so a
real ``Database`` can be built here; anything that would touch the network is
stubbed or monkeypatched. Real-store behavior is covered by the integration
tests.
"""

from typing import Any

import pytest

import adapters.db as db_module
from adapters.db import (
    DENSE_VECTOR,
    ENTITIES_COLLECTION,
    EPISODIC_COLLECTION,
    SPARSE_VECTOR,
    USER_ID_FIELD,
    Database,
    StoreHealth,
)
from config.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, open_router_api_key="test-key")


@pytest.fixture
def db(settings: Settings) -> Database:
    return Database(settings)


# ── pooling / handle reuse (spec §1 rule 1) ──────────────────────────────


def test_mongo_handles_share_one_pooled_client(db: Database) -> None:
    a = db.mongo("collection_a")
    b = db.mongo("collection_b")
    assert a.database.client is b.database.client
    assert a.name == "collection_a"


def test_mongo_uses_configured_database_name(db: Database) -> None:
    assert db.mongo("anything").database.name == "companion"


def test_qdrant_returns_same_client_every_call(db: Database) -> None:
    assert db.qdrant() is db.qdrant()


# ── healthcheck (spec §1 rule 2) ─────────────────────────────────────────


async def test_healthcheck_aggregates_pings(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ok() -> bool:
        return True

    async def down() -> bool:
        return False

    monkeypatch.setattr(db, "_ping_mongo", ok)
    monkeypatch.setattr(db, "_ping_qdrant", down)
    monkeypatch.setattr(db, "_ping_graph", ok)
    assert await db.healthcheck() == StoreHealth(mongo=True, qdrant=False, graph=True)


async def test_ping_returns_false_on_driver_error_instead_of_raising(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BrokenAdmin:
        async def command(self, name: str) -> None:
            raise ConnectionError("mongo down")

    class _BrokenMongo:
        admin = _BrokenAdmin()

    monkeypatch.setattr(db, "_mongo", _BrokenMongo())
    assert await db._ping_mongo() is False


async def test_startup_fails_loudly_naming_unreachable_stores(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unhealthy() -> StoreHealth:
        return StoreHealth(mongo=True, qdrant=False, graph=False)

    monkeypatch.setattr(db, "healthcheck", unhealthy)
    with pytest.raises(RuntimeError, match="qdrant, graph"):
        await db.startup()


async def test_startup_ensures_collections_when_healthy(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def healthy() -> StoreHealth:
        return StoreHealth(mongo=True, qdrant=True, graph=True)

    ensured = False

    async def ensure() -> None:
        nonlocal ensured
        ensured = True

    monkeypatch.setattr(db, "healthcheck", healthy)
    monkeypatch.setattr(db, "ensure_qdrant_collections", ensure)
    await db.startup()
    assert ensured


# ── Qdrant collection bootstrap (spec §1 rule 3) ─────────────────────────


class _RecordingQdrant:
    def __init__(self, existing: set[str]) -> None:
        self.existing = existing
        self.created: list[dict[str, Any]] = []
        self.indexed: list[dict[str, Any]] = []

    async def collection_exists(self, name: str) -> bool:
        return name in self.existing

    async def create_collection(self, **kwargs: Any) -> None:
        self.created.append(kwargs)

    async def create_payload_index(self, **kwargs: Any) -> None:
        self.indexed.append(kwargs)


async def test_creates_missing_collections_with_dense_sparse_and_user_id_index(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    qdrant = _RecordingQdrant(existing=set())
    monkeypatch.setattr(db, "_qdrant", qdrant)

    await db.ensure_qdrant_collections()

    assert [c["collection_name"] for c in qdrant.created] == [
        EPISODIC_COLLECTION,
        ENTITIES_COLLECTION,
    ]
    for created in qdrant.created:
        assert DENSE_VECTOR in created["vectors_config"]
        assert SPARSE_VECTOR in created["sparse_vectors_config"]
    assert all(i["field_name"] == USER_ID_FIELD for i in qdrant.indexed)
    assert len(qdrant.indexed) == 2


async def test_existing_collections_are_not_recreated_but_index_is_ensured(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    qdrant = _RecordingQdrant(existing={EPISODIC_COLLECTION, ENTITIES_COLLECTION})
    monkeypatch.setattr(db, "_qdrant", qdrant)

    await db.ensure_qdrant_collections()

    assert qdrant.created == []
    assert len(qdrant.indexed) == 2


# ── graphiti wiring ──────────────────────────────────────────────────────


class _FakeGraphiti:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def test_graphiti_is_configured_for_openrouter_and_cached(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db_module, "Graphiti", _FakeGraphiti)
    monkeypatch.setattr(db_module, "Neo4jDriver", lambda **kwargs: kwargs)
    db = Database(settings)

    graphiti = db.graphiti()

    assert isinstance(graphiti, _FakeGraphiti)
    assert graphiti.kwargs["llm_client"].config.base_url == settings.open_router_base_url
    assert graphiti.kwargs["llm_client"].config.api_key == "test-key"
    assert db.graphiti() is graphiti  # pooled/reused, not rebuilt


def test_graphiti_fails_loudly_without_openrouter_key() -> None:
    db = Database(Settings(_env_file=None, open_router_api_key=""))
    with pytest.raises(RuntimeError, match="OPEN_ROUTER_API_KEY"):
        db.graphiti()
