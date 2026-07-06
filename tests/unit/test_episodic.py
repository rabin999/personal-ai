"""Unit tests for Episodic Memory (spec §5) — VectorStore port faked."""

from datetime import UTC, datetime, timedelta

from core.memory.episodic import (
    EPISODIC_COLLECTION,
    EpisodicMemory,
    chunk_transcript,
)
from core.memory.working import Turn
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
        self.searches.append(
            {"collection": collection, "query": query_text, "user_id": user_id, "k": k}
        )
        return self.hits


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


# ── write ────────────────────────────────────────────────────────────────


async def test_write_upserts_chunks_with_user_scoped_payload() -> None:
    vectors = FakeVectorStore()
    memory = EpisodicMemory(vectors)

    await memory.write(
        "u_demo_001", "s1", ["chunk one", "chunk two"], meta={"emotion": {"valence": 0.2}}
    )

    collection, docs = vectors.upserts[0]
    assert collection == EPISODIC_COLLECTION
    assert len(docs) == 2
    for doc in docs:
        assert doc.payload["user_id"] == "u_demo_001"
        assert doc.payload["session_id"] == "s1"
        assert doc.payload["timestamp"]
        assert doc.payload["emotion"] == {"valence": 0.2}
    assert docs[0].id != docs[1].id


async def test_write_nothing_for_empty_chunk_list() -> None:
    vectors = FakeVectorStore()
    await EpisodicMemory(vectors).write("u_demo_001", "s1", [])
    assert vectors.upserts[0][1] == []


# ── retrieve ─────────────────────────────────────────────────────────────


async def test_retrieve_is_user_scoped_and_maps_payload() -> None:
    vectors = FakeVectorStore(
        hits=[
            VectorHit(
                id="1",
                score=0.9,
                payload={
                    "text": "we discussed SYPNL",
                    "session_id": "s1",
                    "timestamp": _iso(0),
                },
            )
        ]
    )
    memory = EpisodicMemory(vectors)

    hits = await memory.retrieve("u_demo_001", "what did we say about SYPNL?", k=3)

    assert vectors.searches[0]["user_id"] == "u_demo_001"
    assert vectors.searches[0]["k"] == 3
    assert hits[0].text == "we discussed SYPNL"
    assert hits[0].session_id == "s1"


async def test_recency_weighting_prefers_newer_of_equally_relevant_hits() -> None:
    vectors = FakeVectorStore(
        hits=[
            VectorHit(id="old", score=0.5, payload={"text": "old", "timestamp": _iso(365)}),
            VectorHit(id="new", score=0.5, payload={"text": "new", "timestamp": _iso(0)}),
        ]
    )
    hits = await EpisodicMemory(vectors).retrieve("u_demo_001", "anything")
    assert [h.text for h in hits] == ["new", "old"]


async def test_recency_weighting_never_drowns_relevance() -> None:
    # A strongly relevant year-old memory must still beat a weak fresh one.
    vectors = FakeVectorStore(
        hits=[
            VectorHit(
                id="old-strong", score=0.9, payload={"text": "old", "timestamp": _iso(365)}
            ),
            VectorHit(id="new-weak", score=0.2, payload={"text": "new", "timestamp": _iso(0)}),
        ]
    )
    hits = await EpisodicMemory(vectors).retrieve("u_demo_001", "anything")
    assert hits[0].text == "old"


# ── chunking (rule 1: turn-based, not fixed-size) ────────────────────────


def _turns(*texts: str) -> list[Turn]:
    return [
        Turn(role="user" if i % 2 == 0 else "assistant", text=text)
        for i, text in enumerate(texts)
    ]


def test_chunks_preserve_every_turn_in_order() -> None:
    turns = _turns("alpha", "beta", "gamma", "delta")
    joined = "\n".join(chunk_transcript(turns))
    assert joined.index("alpha") < joined.index("beta") < joined.index("gamma")
    assert "user: alpha" in joined and "assistant: beta" in joined


def test_chunks_break_at_turn_boundaries_not_mid_turn() -> None:
    turns = _turns("x" * 700, "y" * 700, "z" * 700)
    chunks = chunk_transcript(turns, max_chars=1200)
    assert len(chunks) > 1
    for chunk in chunks:
        # every line in a chunk is a complete turn
        for line in chunk.split("\n"):
            assert line.startswith(("user: ", "assistant: "))


def test_single_oversized_turn_becomes_its_own_chunk() -> None:
    turns = _turns("short", "L" * 5000, "short again")
    chunks = chunk_transcript(turns, max_chars=1200)
    assert any("L" * 5000 in chunk for chunk in chunks)


def test_empty_transcript_produces_no_chunks() -> None:
    assert chunk_transcript([]) == []
