"""Unit tests for reranked episodic retrieval (A10)."""

from core.memory.episodic import EpisodicMemory
from ports.vector_store import VectorHit


class FakeVectors:
    def __init__(self, hits):
        self._hits = hits

    async def hybrid_search(self, collection, query_text, *, user_id, k=6):
        return self._hits[:k]

    async def upsert_texts(self, c, d): ...
    async def list_by_user(self, c, *, user_id, limit=100):
        return []

    async def delete(self, c, i, *, user_id):
        return True


class PickSecondReranker:
    """A reranker that always promotes the doc containing 'RELEVANT' to the top."""

    def rerank(self, query, documents, *, top_n):
        order = sorted(range(len(documents)), key=lambda i: "RELEVANT" not in documents[i])
        return order[:top_n]


def _hit(text, score):
    return VectorHit(id=text, score=score, payload={"text": text})


async def test_reranker_reorders_and_truncates() -> None:
    # Fusion order puts the RELEVANT doc last; the reranker must promote it.
    hits = [_hit("noise A", 0.9), _hit("noise B", 0.8), _hit("the RELEVANT one", 0.1)]
    mem = EpisodicMemory(FakeVectors(hits), reranker=PickSecondReranker())  # type: ignore[arg-type]
    out = await mem.retrieve("u", "find the relevant thing", k=1)
    assert len(out) == 1 and "RELEVANT" in out[0].text


async def test_no_reranker_keeps_fusion_recency_order() -> None:
    hits = [_hit("top", 0.9), _hit("mid", 0.5), _hit("low", 0.1)]
    mem = EpisodicMemory(FakeVectors(hits))  # no reranker
    out = await mem.retrieve("u", "q", k=2)
    assert [h.text for h in out] == ["top", "mid"]
