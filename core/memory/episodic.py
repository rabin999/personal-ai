"""Episodic Memory (spec §5): conversations as embedded chunks, ground-truth history.

Write path: session transcripts are chunked at turn boundaries (rule 1 —
semantic units, not fixed-size windows), embedded and upserted per user.
Read path: hybrid dense+BM25 retrieval fused with RRF in the vector store,
then gently recency-weighted here (rule 2).
"""

import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel

from core.memory.working import Turn
from ports.vector_store import VectorDoc, VectorStore

EPISODIC_COLLECTION = "episodic"

# Dedup key normalization: same fact phrased slightly differently ("bought 10
# shares of SYPNL at 230" / "user bought 10 shares of SYPNL at $230") collapses to
# one key. Deliberately high-precision (exact normalized match) so distinct events
# are never merged — currency/user-prefix/punctuation noise only.
_DEDUP_STRIP = re.compile(r"[^a-z0-9 ]+")
_DEDUP_LEAD = re.compile(r"^(the )?user[:\s]+")


def _dedup_key(text: str) -> str:
    t = text.strip().lower()
    t = _DEDUP_LEAD.sub("", t)  # drop a leading "user:"/"user " subject prefix
    t = _DEDUP_STRIP.sub(" ", t)  # currency signs, punctuation → space
    return re.sub(r"\s+", " ", t).strip()


# Chunking: accumulate whole turns until the budget is reached. Large enough
# to keep an exchange together, small enough to stay a focused retrieval unit.
MAX_CHUNK_CHARS = 1200

# Recency weighting: half-life decay blended at 30% so an old-but-highly-
# relevant memory still outranks a recent-but-weak one (rule 2 SHOULD).
RECENCY_HALF_LIFE_DAYS = 30.0
RECENCY_BLEND = 0.3


class EpisodicHit(BaseModel):
    text: str
    session_id: str | None = None
    timestamp: str | None = None
    score: float
    emotion: dict[str, Any] | None = None
    id: str | None = None  # vector-store point id (for delete from /memories)


class _Reranker(Protocol):
    def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[int]: ...


class EpisodicMemory:
    def __init__(self, vectors: VectorStore, reranker: "_Reranker | None" = None) -> None:
        self._vectors = vectors
        # A10: optional cross-encoder reranker to pick which fused candidates enter
        # the prompt (improves context quality). None → fusion + recency order.
        self._reranker = reranker

    async def write(
        self,
        user_id: str,
        session_id: str,
        chunks: list[str],
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        """Embed and upsert transcript chunks, payload-scoped to the user."""
        meta = dict(meta or {})
        timestamp = meta.get("timestamp") or datetime.now(UTC).isoformat()
        docs = []
        for chunk in chunks:
            payload: dict[str, Any] = {
                "user_id": user_id,
                "session_id": session_id,
                "timestamp": timestamp,
            }
            if meta.get("emotion") is not None:
                payload["emotion"] = meta["emotion"]
            docs.append(VectorDoc(id=str(uuid.uuid4()), text=chunk, payload=payload))
        await self._vectors.upsert_texts(EPISODIC_COLLECTION, docs)

    async def list_recent(self, user_id: str, limit: int = 50) -> list[EpisodicHit]:
        """All of a user's stored episodic memories, newest first (for /memories)."""
        hits = await self._vectors.list_by_user(EPISODIC_COLLECTION, user_id=user_id, limit=limit)
        items = [
            EpisodicHit(
                text=str(h.payload.get("text", "")),
                session_id=h.payload.get("session_id"),
                timestamp=h.payload.get("timestamp"),
                score=0.0,
                emotion=h.payload.get("emotion"),
                id=h.id,
            )
            for h in hits
        ]
        items.sort(key=lambda e: e.timestamp or "", reverse=True)
        return items

    async def delete_all(self, user_id: str) -> None:
        """Delete ALL of this user's episodic memories (account deletion)."""
        await self._vectors.delete_all_for_user(EPISODIC_COLLECTION, user_id=user_id)

    async def delete(self, user_id: str, memory_id: str) -> bool:
        """Delete one of this user's episodic memories (the 'forget this' right)."""
        return await self._vectors.delete(EPISODIC_COLLECTION, memory_id, user_id=user_id)

    async def deduplicate(self, user_id: str, limit: int = 500) -> int:
        """Collapse exact near-duplicate episodic entries (spec §5 consolidation).

        Broken extraction runs accreted the SAME event several times ("bought 10
        shares of SYPNL at 230" x3, "headache right now" x2). Group by a normalized
        key, keep the EARLIEST entry of each group (canonical, preserves history —
        rule: never lose the original), delete the rest. Returns entries removed.
        High-precision (exact normalized match) so genuinely distinct events are
        never merged. user_id-scoped; only ever touches this user's data."""
        entries = await self.list_recent(user_id, limit=limit)
        by_key: dict[str, list[EpisodicHit]] = {}
        for e in entries:
            if e.id and e.text.strip():
                by_key.setdefault(_dedup_key(e.text), []).append(e)
        removed = 0
        for group in by_key.values():
            if len(group) < 2:
                continue
            # Keep the earliest (oldest timestamp); delete the newer duplicates.
            group.sort(key=lambda e: e.timestamp or "")
            for dup in group[1:]:
                assert dup.id is not None  # filtered above
                if await self.delete(user_id, dup.id):
                    removed += 1
        return removed

    async def retrieve(self, user_id: str, query_text: str, k: int = 6) -> list[EpisodicHit]:
        """Hybrid RRF retrieval (adapter) + recency weighting, user-scoped. With a
        reranker (A10), fetch a wider candidate set and let a cross-encoder pick the
        top-k most relevant to the query — better context than fusion score alone."""
        fetch_k = k * 3 if self._reranker is not None else k
        hits = await self._vectors.hybrid_search(
            EPISODIC_COLLECTION, query_text, user_id=user_id, k=fetch_k
        )
        now = datetime.now(UTC)
        scored = [
            EpisodicHit(
                text=str(hit.payload.get("text", "")),
                session_id=hit.payload.get("session_id"),
                timestamp=hit.payload.get("timestamp"),
                emotion=hit.payload.get("emotion"),
                score=hit.score * _recency_weight(hit.payload.get("timestamp"), now),
            )
            for hit in hits
        ]
        scored.sort(key=lambda h: h.score, reverse=True)
        if self._reranker is not None and len(scored) > k:
            order = self._reranker.rerank(query_text, [h.text for h in scored], top_n=k)
            return [scored[i] for i in order]
        return scored[:k]


def chunk_transcript(turns: list[Turn], max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Group consecutive turns into chunks, breaking only at turn boundaries."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for turn in turns:
        line = f"{turn.role}: {turn.text}"
        if current and current_len + len(line) > max_chars:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _recency_weight(timestamp: str | None, now: datetime) -> float:
    if not timestamp:
        return 1.0
    try:
        age_days = (now - datetime.fromisoformat(timestamp)).total_seconds() / 86_400
    except ValueError:
        return 1.0
    decay: float = 0.5 ** (max(age_days, 0.0) / RECENCY_HALF_LIFE_DAYS)
    return (1.0 - RECENCY_BLEND) + RECENCY_BLEND * decay
