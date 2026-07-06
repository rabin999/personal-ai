"""Entity Resolution (spec §8): vague references → concrete stored IDs.

"How's my trading thing" must pull the right project. Each entity keeps a
searchable pointer (name + description) in the vector store; canonical data
stays in Mongo/Graphiti. Resolution is hybrid dense+BM25, user-filtered.

Rule 3 mechanism: ``is_ambiguous`` tells the caller (§12) whether the top
candidates are too close to resolve silently — the disambiguation question
itself is response-generation behavior, not decided here.
"""

import uuid

from pydantic import BaseModel

from ports.vector_store import VectorDoc, VectorStore

ENTITIES_COLLECTION = "entities"

# A runner-up within this fraction of the top score is "close" → ambiguous.
CLOSE_SCORE_RATIO = 0.8

# Minimum fused (dense+BM25 RRF) score to treat a hit as a real reference
# (V-ENTITY-1). Empirically a confident match scores ~1.0 while an unrelated
# phrase tops out ~0.5, so a mid-gap floor rejects "resolve to the nearest
# entity for any phrase". Tunable; below it, resolution returns nothing.
MIN_RESOLUTION_SCORE = 0.6

_POINT_NAMESPACE = uuid.UUID("6d0cbe8e-4d5f-4a51-9e0f-1f2c3d4e5a6b")


class EntityCandidate(BaseModel):
    entity_id: str
    entity_type: str
    name: str
    score: float


class EntityResolver:
    def __init__(self, vectors: VectorStore) -> None:
        self._vectors = vectors

    async def index(
        self, user_id: str, entity_type: str, entity_id: str, name: str, description: str
    ) -> None:
        """Create or update an entity pointer (rule 1: rename = re-index).

        The point id is derived from (user, type, id), so re-indexing the
        same entity overwrites its pointer instead of duplicating it.
        """
        point_id = str(uuid.uuid5(_POINT_NAMESPACE, f"{user_id}:{entity_type}:{entity_id}"))
        doc = VectorDoc(
            id=point_id,
            text=f"{name}\n{description}",
            payload={
                "user_id": user_id,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "name": name,
            },
        )
        await self._vectors.upsert_texts(ENTITIES_COLLECTION, [doc])

    async def resolve(
        self, user_id: str, phrase: str, k: int = 3, min_score: float = MIN_RESOLUTION_SCORE
    ) -> list[EntityCandidate]:
        """Hybrid dense+BM25 candidates for a phrase, this user only (rule 2).

        Candidates below ``min_score`` are dropped: an unrelated phrase resolves
        to nothing rather than to the nearest entity (V-ENTITY-1).
        """
        hits = await self._vectors.hybrid_search(
            ENTITIES_COLLECTION, phrase, user_id=user_id, k=k
        )
        return [
            EntityCandidate(
                entity_id=str(hit.payload.get("entity_id", "")),
                entity_type=str(hit.payload.get("entity_type", "")),
                name=str(hit.payload.get("name", "")),
                score=hit.score,
            )
            for hit in hits
            if hit.score >= min_score
        ]


def is_ambiguous(candidates: list[EntityCandidate]) -> bool:
    """True when the runner-up is close enough that §12 should ask, not guess."""
    if len(candidates) < 2:
        return False
    top, runner_up = candidates[0], candidates[1]
    return runner_up.score >= top.score * CLOSE_SCORE_RATIO
