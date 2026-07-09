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

    async def list_by_user(
        self, collection: str, *, user_id: str, limit: int = 100
    ) -> list[VectorHit]:
        return list(self.hits)

    async def delete(self, collection: str, doc_id: str, *, user_id: str) -> bool:
        return True


async def test_index_writes_pointer_with_payload_and_stable_id() -> None:
    vectors = FakeVectorStore()
    resolver = EntityResolver(vectors)

    await resolver.index(
        "u_demo_001", "project", "proj_1", "NEPSE Portfolio", "stock trading tracker"
    )
    await resolver.index("u_demo_001", "project", "proj_1", "NEPSE Portfolio v2", "renamed tracker")

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


def _hit(entity_id: str, score: float, entity_type: str = "holding") -> VectorHit:
    return VectorHit(
        id=entity_id,
        score=score,
        payload={"entity_id": entity_id, "entity_type": entity_type, "name": entity_id.upper()},
    )


async def test_resolve_returns_candidates_best_first_whatever_order_the_store_gave() -> None:
    """A silent WRONG resolution is a critical failure (spec §8), and every caller reads this
    list POSITIONALLY: `is_ambiguous()` compares `candidates[0]` to `candidates[1]`, and the
    assembler treats the first as the resolved entity.

    Nothing asserted the order. `docs/TEST_AUDIT.md` §6 named this as an unproven invariant,
    and the mutation `entity_resolution_ignores_the_score_order` proves the gap was real: a
    reversed list changed no test, because every case in `gs2_entities.json` yields exactly one
    candidate above `MIN_RESOLUTION_SCORE`, and reversing a one-element list is a no-op.
    """
    vectors = FakeVectorStore([_hit("op", 0.67), _hit("portfolio", 1.0, "project")])
    resolver = EntityResolver(vectors)

    candidates = await resolver.resolve("u1", "my portfolio")

    assert [c.entity_id for c in candidates] == ["portfolio", "op"]
    assert not is_ambiguous(candidates)  # 0.67 < 0.8 * 1.0


async def test_a_dominant_candidate_is_never_shadowed_by_a_weaker_one() -> None:
    """The user's project must win over a holding inside it, however the store ranked them."""
    vectors = FakeVectorStore(
        [_hit("sypnl", 0.75), _hit("op", 0.7), _hit("portfolio", 1.0, "project")]
    )
    resolver = EntityResolver(vectors)

    candidates = await resolver.resolve("u1", "my portfolio")

    assert candidates[0].entity_id == "portfolio"
    assert candidates[0].entity_type == "project"
