"""Integration tests for Episodic Memory (spec §5) — real Qdrant + real embeddings.

First run downloads the fastembed models (one-time, cached in ~/.cache).
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from qdrant_client import models

from adapters.db import EPISODIC_COLLECTION, USER_ID_FIELD, Database
from adapters.vector.qdrant import QdrantVectorStore
from config.settings import Settings
from core.memory.episodic import EpisodicMemory

pytestmark = pytest.mark.integration


@pytest.fixture
async def memory(db: Database) -> AsyncIterator[EpisodicMemory]:
    settings = Settings(_env_file=None)
    # Recreate the collection if an older run left a different vector size.
    client = db.qdrant()
    if await client.collection_exists(EPISODIC_COLLECTION):
        info = await client.get_collection(EPISODIC_COLLECTION)
        vectors = info.config.params.vectors
        assert isinstance(vectors, dict)
        if vectors["dense"].size != settings.embedding_dim:
            await client.delete_collection(EPISODIC_COLLECTION)
    await db.ensure_qdrant_collections()

    yield EpisodicMemory(QdrantVectorStore(db, settings.embedding_model))

    await client.delete(
        EPISODIC_COLLECTION,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key=USER_ID_FIELD, match=models.MatchText(text="it_")
                    )
                ]
            )
        ),
    )


@pytest.fixture
def user_id() -> str:
    return f"it_{uuid.uuid4().hex[:12]}"


CHUNKS = [
    "user: I finally bought 20 shares of SYPNL at 42 dollars\nassistant: nice, "
    "that's the biotech ticker you were watching",
    "user: my sister Maya is visiting next weekend and we want to go hiking\n"
    "assistant: the trail by the lake should be great in this weather",
    "user: work has been exhausting lately, too many late nights\nassistant: "
    "that sounds draining — anything you can hand off?",
]


# Acceptance: writing a transcript produces retrievable chunks scoped to user.
async def test_written_chunks_are_retrievable_for_the_writer(
    memory: EpisodicMemory, user_id: str
) -> None:
    await memory.write(user_id, "s1", CHUNKS)
    hits = await memory.retrieve(user_id, "stocks I bought")
    assert hits and any("SYPNL" in h.text for h in hits)


# Acceptance: exact keyword hits via BM25 even if semantically distant.
async def test_exact_keyword_retrieves_via_bm25(memory: EpisodicMemory, user_id: str) -> None:
    await memory.write(user_id, "s1", CHUNKS)
    hits = await memory.retrieve(user_id, "SYPNL", k=3)
    assert hits and "SYPNL" in hits[0].text


# Acceptance: paraphrased query hits the semantically relevant chunk (dense).
async def test_paraphrase_retrieves_via_dense_semantics(
    memory: EpisodicMemory, user_id: str
) -> None:
    await memory.write(user_id, "s1", CHUNKS)
    hits = await memory.retrieve(user_id, "feeling burned out from my job", k=1)
    assert hits and "exhausting" in hits[0].text


# Acceptance: RRF fusion — both signals contribute to one result set.
async def test_rrf_fuses_keyword_and_semantic_signals(
    memory: EpisodicMemory, user_id: str
) -> None:
    await memory.write(user_id, "s1", CHUNKS)
    hits = await memory.retrieve(user_id, "did I tell you about SYPNL and my family plans?")
    texts = " | ".join(h.text for h in hits)
    assert "SYPNL" in texts  # keyword leg
    assert "Maya" in texts  # semantic leg


# Acceptance: results never include another user_id.
async def test_two_user_isolation_in_retrieval(memory: EpisodicMemory, user_id: str) -> None:
    other = f"it_{uuid.uuid4().hex[:12]}"
    await memory.write(user_id, "s1", CHUNKS)
    await memory.write(other, "s2", ["user: my secret project is called ZEPHYR9"])

    for query in ("ZEPHYR9", "secret project"):
        hits = await memory.retrieve(user_id, query)
        assert all("ZEPHYR9" not in h.text for h in hits)
