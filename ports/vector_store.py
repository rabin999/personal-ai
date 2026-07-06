"""Port: vector store (Qdrant adapter) — episodic memory + entity pointers (spec §1, §5, §8).

The adapter owns embedding (dense + sparse) so core stays provider-agnostic;
core hands over raw text and gets scored payloads back. Every search is
``user_id``-filtered inside the adapter — isolation is not optional.
"""

from typing import Any, Protocol

from pydantic import BaseModel, Field


class VectorDoc(BaseModel):
    id: str
    text: str
    payload: dict[str, Any] = Field(default_factory=dict)


class VectorHit(BaseModel):
    id: str
    score: float
    payload: dict[str, Any] = Field(default_factory=dict)


class VectorStore(Protocol):
    async def upsert_texts(self, collection: str, docs: list[VectorDoc]) -> None:
        """Embed each doc's text (dense + sparse) and upsert with its payload."""
        ...

    async def hybrid_search(
        self, collection: str, query_text: str, *, user_id: str, k: int = 6
    ) -> list[VectorHit]:
        """Dense + BM25 sub-queries fused with RRF, filtered to ``user_id``."""
        ...
