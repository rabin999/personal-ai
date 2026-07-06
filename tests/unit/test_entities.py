"""Unit tests for Entity Resolution (spec §8) — VectorStore faked."""

from core.memory.entities import (
    ENTITIES_COLLECTION,
    EntityCandidate,
    EntityResolver,
    is_ambiguous,
)
from ports.vector_store import VectorDoc, VectorHit


class FakeVectorStore:
    def __init__(self, hits: list[VectorHit] | None = None) -> None:
        self.upserts: list[tuple[str, list[VectorDoc]]] = []
        self.searches: list[dict[str, object]] = []
        self.hits = hits or []

    async def upsert_texts(self, collection: str, docs: list[VectorDoc]) -> None:
        self.upserts.append((collection, docs))

    async def hybrid_search(
        self, collection: str, query_text: str, *, user_id: str, k: int = 6
    ) -> list[VectorHit]:
        self.searches.append({"collection": collection, "user_id": user_id, "k": k})
        return self.hits


async def test_index_writes_pointer_with_payload_and_stable_id() -> None:
    vectors = FakeVectorStore()
    resolver = EntityResolver(vectors)

    await resolver.index(
        "u_demo_001", "project", "proj_1", "NEPSE Portfolio", "stock trading tracker"
    )
    await resolver.index(
        "u_demo_001", "project", "proj_1", "NEPSE Portfolio v2", "renamed tracker"
    )

    (_, first), (_, second) = vectors.upserts
    assert first[0].payload["user_id"] == "u_demo_001"
    assert first[0].payload["entity_id"] == "proj_1"
    assert first[0].payload["name"] == "NEPSE Portfolio"
    # Rename re-indexes under the same point id → update, not duplicate.
    assert first[0].id == second[0].id
    assert second[0].payload["name"] == "NEPSE Portfolio v2"


async def test_same_entity_id_for_different_users_gets_different_points() -> None:
    vectors = FakeVectorStore()
    resolver = EntityResolver(vectors)
    await resolver.index("u_demo_001", "project", "proj_1", "A", "d")
    await resolver.index("u_demo_002", "project", "proj_1", "A", "d")
    (_, first), (_, second) = vectors.upserts
    assert first[0].id != second[0].id


async def test_resolve_is_user_scoped_and_maps_candidates() -> None:
    vectors = FakeVectorStore(
        hits=[
            VectorHit(
                id="p1",
                score=0.9,
                payload={
                    "entity_id": "proj_1",
                    "entity_type": "project",
                    "name": "NEPSE Portfolio",
                },
            )
        ]
    )
    resolver = EntityResolver(vectors)

    candidates = await resolver.resolve("u_demo_001", "my trading thing")

    assert vectors.searches[0] == {
        "collection": ENTITIES_COLLECTION,
        "user_id": "u_demo_001",
        "k": 3,
    }
    assert candidates[0].entity_id == "proj_1"
    assert candidates[0].entity_type == "project"


def _candidate(name: str, score: float) -> EntityCandidate:
    return EntityCandidate(entity_id=name, entity_type="project", name=name, score=score)


def test_close_candidates_are_ambiguous_dominant_one_is_not() -> None:
    assert is_ambiguous([_candidate("a", 0.50), _candidate("b", 0.48)])
    assert not is_ambiguous([_candidate("a", 0.90), _candidate("b", 0.30)])
    assert not is_ambiguous([_candidate("a", 0.90)])
    assert not is_ambiguous([])
