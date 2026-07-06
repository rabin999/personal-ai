"""Episodic Memory (spec §5): conversations as embedded chunks, ground-truth history.

Write path: session transcripts are chunked at turn boundaries (rule 1 —
semantic units, not fixed-size windows), embedded and upserted per user.
Read path: hybrid dense+BM25 retrieval fused with RRF in the vector store,
then gently recency-weighted here (rule 2).
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from core.memory.working import Turn
from ports.vector_store import VectorDoc, VectorStore

EPISODIC_COLLECTION = "episodic"

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


class EpisodicMemory:
    def __init__(self, vectors: VectorStore) -> None:
        self._vectors = vectors

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

    async def retrieve(self, user_id: str, query_text: str, k: int = 6) -> list[EpisodicHit]:
        """Hybrid RRF retrieval (adapter) + recency weighting, user-scoped."""
        hits = await self._vectors.hybrid_search(
            EPISODIC_COLLECTION, query_text, user_id=user_id, k=k
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
        return scored


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
