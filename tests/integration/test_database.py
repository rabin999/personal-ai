"""Integration tests for the Database Layer (spec §1) against real stores."""

import asyncio
import uuid
from typing import Any

import pytest
from qdrant_client import models

from adapters.db import (
    DENSE_VECTOR,
    ENTITIES_COLLECTION,
    EPISODIC_COLLECTION,
    SPARSE_VECTOR,
    USER_ID_FIELD,
    Database,
)

pytestmark = pytest.mark.integration


# Acceptance: healthcheck returns all-true against running Mongo/Qdrant/Neo4j.
async def test_healthcheck_all_true_against_running_stores(db: Database) -> None:
    assert await db.healthcheck() == {"mongo": True, "qdrant": True, "graph": True}


# Acceptance: episodic + entities collections exist with sparse+dense vector
# config and a user_id payload index.
async def test_qdrant_collections_created_with_hybrid_vectors_and_user_id_index(
    db: Database,
) -> None:
    await db.ensure_qdrant_collections()

    for name in (EPISODIC_COLLECTION, ENTITIES_COLLECTION):
        info = await db.qdrant().get_collection(name)
        vectors = info.config.params.vectors
        assert isinstance(vectors, dict) and DENSE_VECTOR in vectors
        sparse = info.config.params.sparse_vectors
        assert sparse is not None and SPARSE_VECTOR in sparse
        assert sparse[SPARSE_VECTOR].modifier == models.Modifier.IDF
        user_id_index = info.payload_schema.get(USER_ID_FIELD)
        assert user_id_index is not None
        assert user_id_index.data_type == models.PayloadSchemaType.KEYWORD


async def test_ensure_qdrant_collections_is_idempotent(db: Database) -> None:
    await db.ensure_qdrant_collections()
    await db.ensure_qdrant_collections()  # second run must not raise or recreate


async def test_startup_sequence_succeeds(db: Database) -> None:
    await db.startup()


async def test_mongo_handle_round_trips_a_document(db: Database) -> None:
    collection = db.mongo("_it_dbcheck")
    doc_id = str(uuid.uuid4())
    try:
        await collection.insert_one({"_id": doc_id, "probe": True})
        found = await collection.find_one({"_id": doc_id})
        assert found is not None and found["probe"] is True
    finally:
        await collection.delete_many({"_id": doc_id})


# Acceptance: repeated calls reuse pooled connections (no leak under load).
async def test_no_connection_leak_under_concurrent_load(db: Database) -> None:
    async def one_op(i: int) -> None:
        await db.healthcheck()
        await db.mongo("_it_dbcheck").find_one({"_id": f"missing-{i}"})

    async def connections_created() -> int:
        status: dict[str, Any] = await db.mongo("_it_dbcheck").database.client.admin.command(
            "serverStatus"
        )
        total: int = status["connections"]["totalCreated"]
        return total

    # A pooled client opens at most ~maxPoolSize (100) sockets no matter how
    # many operations run; per-call connects would open one per operation.
    # 500 ops staying under 150 new connections proves reuse, with margin for
    # pool ramp-up and unrelated clients.
    baseline = await connections_created()
    for _ in range(10):
        await asyncio.gather(*(one_op(i) for i in range(50)))
    created_during_load = await connections_created() - baseline

    assert created_during_load < 150
