"""Integration tests for Entity Resolution (spec §8) — real Qdrant + embeddings."""

import uuid
from collections.abc import AsyncIterator

import pytest
from qdrant_client import models

from adapters.db import ENTITIES_COLLECTION, USER_ID_FIELD, Database
from adapters.vector.qdrant import QdrantVectorStore
from config.settings import Settings
from core.memory.entities import EntityResolver, is_ambiguous

pytestmark = pytest.mark.integration


@pytest.fixture
async def resolver(db: Database) -> AsyncIterator[EntityResolver]:
    settings = Settings(_env_file=None)
    client = db.qdrant()
    if await client.collection_exists(ENTITIES_COLLECTION):
        info = await client.get_collection(ENTITIES_COLLECTION)
        vectors = info.config.params.vectors
        assert isinstance(vectors, dict)
        if vectors["dense"].size != settings.embedding_dim:
            await client.delete_collection(ENTITIES_COLLECTION)
    await db.ensure_qdrant_collections()

    yield EntityResolver(QdrantVectorStore(db, settings.embedding_model))

    await client.delete(
        ENTITIES_COLLECTION,
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


async def _index_portfolio(resolver: EntityResolver, user_id: str) -> None:
    await resolver.index(
        user_id,
        "project",
        "proj_nepse",
        "NEPSE Portfolio",
        "tracking my stock trades and investments on the Nepal stock exchange",
    )


# Acceptance: "my trading thing" resolves to the finance project when only
# one exists.
async def test_vague_phrase_resolves_to_the_only_finance_project(
    resolver: EntityResolver, user_id: str
) -> None:
    await _index_portfolio(resolver, user_id)
    await resolver.index(
        user_id, "project", "proj_garden", "Garden Planner", "planning vegetable beds and watering"
    )

    candidates = await resolver.resolve(user_id, "how's my trading thing")

    assert candidates and candidates[0].entity_id == "proj_nepse"
    assert not is_ambiguous(candidates)  # one project dominates → silent resolve


# Acceptance: two similar projects produce two close candidates.
async def test_two_similar_projects_trigger_disambiguation_path(
    resolver: EntityResolver, user_id: str
) -> None:
    await _index_portfolio(resolver, user_id)
    await resolver.index(
        user_id,
        "project",
        "proj_us_stocks",
        "US Stocks Tracker",
        "tracking my stock trades and investments on the US stock market",
    )

    candidates = await resolver.resolve(user_id, "how's my trading thing")

    assert len(candidates) >= 2
    top_ids = {candidates[0].entity_id, candidates[1].entity_id}
    assert top_ids == {"proj_nepse", "proj_us_stocks"}
    assert is_ambiguous(candidates)  # close scores → §12 asks instead of guessing


# Acceptance: exact name match resolves via BM25 even with semantic noise.
async def test_exact_name_resolves_via_bm25_despite_semantic_noise(
    resolver: EntityResolver, user_id: str
) -> None:
    await _index_portfolio(resolver, user_id)
    for i, (name, desc) in enumerate(
        [
            ("Portfolio Website", "personal site showcasing design work"),
            ("Reading List", "books to read this year"),
            ("Meal Prep", "weekly cooking and grocery planning"),
        ]
    ):
        await resolver.index(user_id, "project", f"proj_noise_{i}", name, desc)

    candidates = await resolver.resolve(user_id, "NEPSE Portfolio")

    assert candidates and candidates[0].entity_id == "proj_nepse"


async def test_rename_updates_the_pointer_in_place(
    resolver: EntityResolver, user_id: str
) -> None:
    await _index_portfolio(resolver, user_id)
    await resolver.index(
        user_id,
        "project",
        "proj_nepse",
        "Sherpa Capital",
        "tracking my stock trades and investments on the Nepal stock exchange",
    )

    candidates = await resolver.resolve(user_id, "Sherpa Capital")
    assert candidates[0].entity_id == "proj_nepse"
    assert candidates[0].name == "Sherpa Capital"
    # Old name no longer present as a separate entity for this user.
    all_for_user = await resolver.resolve(user_id, "stock trades Nepal", k=5)
    assert len([c for c in all_for_user if c.entity_id == "proj_nepse"]) == 1


async def test_two_user_isolation_in_resolution(
    resolver: EntityResolver, user_id: str
) -> None:
    other = f"it_{uuid.uuid4().hex[:12]}"
    await _index_portfolio(resolver, user_id)
    await resolver.index(
        other, "project", "proj_secret", "Skunkworks X", "confidential prototype"
    )

    for phrase in ("Skunkworks X", "confidential prototype"):
        candidates = await resolver.resolve(user_id, phrase)
        assert all(c.entity_id != "proj_secret" for c in candidates)
