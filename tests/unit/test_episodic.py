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

    async def list_by_user(
        self, collection: str, *, user_id: str, limit: int = 100
    ) -> list[VectorHit]:
        return list(self.hits)

    async def delete(self, collection: str, doc_id: str, *, user_id: str) -> bool:
        return True


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
            VectorHit(id="old-strong", score=0.9, payload={"text": "old", "timestamp": _iso(365)}),
            VectorHit(id="new-weak", score=0.2, payload={"text": "new", "timestamp": _iso(0)}),
        ]
    )
    hits = await EpisodicMemory(vectors).retrieve("u_demo_001", "anything")
    assert hits[0].text == "old"


# ── chunking (rule 1: turn-based, not fixed-size) ────────────────────────


def _turns(*texts: str) -> list[Turn]:
    return [
        Turn(role="user" if i % 2 == 0 else "assistant", text=text) for i, text in enumerate(texts)
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


# ── dedup / consolidation (spec §5) ────────────────────────────────────────


class DedupFakeStore:
    """Tracks deletions and returns seeded entries so dedup can be asserted."""

    def __init__(self, entries: list[VectorHit]) -> None:
        self.entries = entries
        self.deleted: list[str] = []

    async def list_by_user(
        self, collection: str, *, user_id: str, limit: int = 100
    ) -> list[VectorHit]:
        return [e for e in self.entries if e.id not in self.deleted]

    async def delete(self, collection: str, doc_id: str, *, user_id: str) -> bool:
        self.deleted.append(doc_id)
        return True


def _hit(doc_id: str, text: str, days_ago: float) -> VectorHit:
    return VectorHit(id=doc_id, score=0.0, payload={"text": text, "timestamp": _iso(days_ago)})


def test_dedup_key_normalizes_currency_prefix_and_punctuation() -> None:
    from core.memory.episodic import _dedup_key

    k = _dedup_key("bought 10 shares of SYPNL at 230")
    assert _dedup_key("user bought 10 shares of SYPNL at 230") == k
    assert _dedup_key("bought 10 shares of SYPNL at $230") == k
    assert _dedup_key("  Bought 10 shares of SYPNL at 230!!  ") == k
    # Distinct events keep distinct keys.
    assert _dedup_key("bought 20 shares of SYPNL at 230") != k


async def test_deduplicate_keeps_earliest_and_removes_duplicates() -> None:
    entries = [
        _hit("a", "user bought 10 shares of SYPNL at 230", days_ago=2),
        _hit("b", "bought 10 shares of SYPNL at $230", days_ago=1),  # dup (newer)
        _hit("c", "bought 10 shares of SYPNL at 230", days_ago=0.5),  # dup (newest)
        _hit("d", "has a headache right now", days_ago=3),
        _hit("e", "user has a headache right now", days_ago=1),  # dup (newer)
        _hit("f", "user goes for a run every morning", days_ago=1),  # unique
    ]
    store = DedupFakeStore(entries)
    memory = EpisodicMemory(store)  # type: ignore[arg-type]

    removed = await memory.deduplicate("u_demo_001")

    assert removed == 3  # 2 SYPNL dups + 1 headache dup
    # Earliest of each group is kept; newer duplicates deleted.
    assert set(store.deleted) == {"b", "c", "e"}
    remaining = {e.id for e in entries if e.id not in store.deleted}
    assert remaining == {"a", "d", "f"}  # canonical trade, canonical headache, unique run


async def test_deduplicate_noop_when_all_unique() -> None:
    entries = [_hit("a", "went hiking", 2), _hit("b", "got a promotion", 1)]
    store = DedupFakeStore(entries)
    removed = await EpisodicMemory(store).deduplicate("u")  # type: ignore[arg-type]
    assert removed == 0 and store.deleted == []
