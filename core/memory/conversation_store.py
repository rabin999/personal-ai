"""Durable raw conversation log (spec §5/§6; brief §6: store ALL conversations).

A verbatim, append-only record of every turn — separate from *derived* memory
(episodic embeddings §5, semantic facts §6). Nothing is ever lost: the raw
history stays fully queryable even if a consolidation/embedding step fails or is
tuned. This is what the user's ``/conversations`` view reads from.

Multi-tenant isolation (§0.5): every write carries ``user_id`` and every read is
``user_id``-scoped. Writes are best-effort and never block or break a turn.

Timestamps are stored as both an epoch ``ts`` (range queries / ordering) and an
ISO ``created_at`` (display), so server-side datetime-range filtering needs no
client-side date math.
"""

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from ports.doc_store import DocStore

logger = logging.getLogger(__name__)

CONVERSATIONS_COLLECTION = "conversations"
CONVERSATION_TURNS_COLLECTION = "conversation_turns"


class ConversationStore:
    def __init__(self, docs: DocStore) -> None:
        self._docs = docs

    async def record_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        turn_index: int,
        user_text: str,
        assistant_text: str,
        trace_turn: int | None = None,
        emotion: dict[str, Any] | None = None,
    ) -> None:
        """Append one exchange (user + assistant) and upsert the session header.

        Each turn is written with ``routed=False``: the raw log is the durable,
        never-lost record (spec §6), and the episodic/semantic/procedural ROUTING
        decision is made LATER by the background memory-routing worker reading
        unrouted turns via this cursor (Item 9) — so routing never blocks the reply
        and a turn is routed exactly once (no double-write)."""
        now = time.time()
        try:
            # String id (not insert's ObjectId) so the routing worker can advance
            # the watermark via get/put on both Mongo and the in-memory fake.
            turn_uid = uuid.uuid4().hex
            await self._docs.put(
                CONVERSATION_TURNS_COLLECTION,
                turn_uid,
                {
                    "user_id": user_id,
                    "session_id": session_id,
                    "turn_index": turn_index,
                    "user_text": user_text,
                    "assistant_text": assistant_text,
                    "trace_turn": trace_turn,
                    "emotion": emotion,
                    "ts": now,
                    "created_at": datetime.now(UTC).isoformat(),
                    "routed": False,  # memory-routing worker will set True (Item 9)
                },
            )
            await self._touch_session(user_id, session_id, now)
        except Exception:  # durability is best-effort; never break the turn
            logger.exception("conversation persistence failed")

    async def unrouted_turns(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Oldest-first turns not yet routed to long-term memory (the cursor).

        The routing worker claims these, routes each once, and marks them routed —
        so re-processing is impossible and double-writes can't happen (Item 9)."""
        docs = await self._docs.find(CONVERSATION_TURNS_COLLECTION, {"routed": False}, limit=100000)
        docs.sort(key=lambda d: d.get("ts", 0.0))
        return docs[:limit]

    async def mark_routed(self, turn_id: str) -> None:
        """Mark one raw-log turn as routed (idempotent watermark advance)."""
        doc = await self._docs.get(CONVERSATION_TURNS_COLLECTION, turn_id)
        if doc is not None:
            doc["routed"] = True
            await self._docs.put(CONVERSATION_TURNS_COLLECTION, turn_id, doc)

    async def recent_raw_turns(self, user_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """This user's most-recent raw turns across sessions (newest first).

        Retrieval consults this so a fact from a just-ended session is recallable
        BEFORE the routing worker has promoted it — closing the read-your-own-
        writes gap (Item 9)."""
        docs = await self._docs.find(
            CONVERSATION_TURNS_COLLECTION, {"user_id": user_id}, limit=100000
        )
        docs.sort(key=lambda d: d.get("ts", 0.0), reverse=True)
        return [_jsonable(d) for d in docs[:limit]]

    async def _touch_session(self, user_id: str, session_id: str, now: float) -> None:
        existing = await self._docs.get(CONVERSATIONS_COLLECTION, session_id)
        header = {
            "_id": session_id,
            "user_id": user_id,
            "session_id": session_id,
            "started_at": existing.get("started_at", now) if existing else now,
            "started_at_iso": (
                existing.get("started_at_iso") if existing else datetime.now(UTC).isoformat()
            ),
            "last_ts": now,
            "last_at_iso": datetime.now(UTC).isoformat(),
            "turn_count": (existing.get("turn_count", 0) if existing else 0) + 1,
        }
        await self._docs.put(CONVERSATIONS_COLLECTION, session_id, header)

    async def list_conversations(
        self,
        user_id: str,
        *,
        offset: int = 0,
        limit: int = 20,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """This user's conversations, newest first, with server-side range filter.

        Returns ``(page, total_matching)`` for paginated rendering.
        """
        docs = await self._docs.find(CONVERSATIONS_COLLECTION, {"user_id": user_id}, limit=100000)
        if start_ts is not None:
            docs = [d for d in docs if d.get("last_ts", 0.0) >= start_ts]
        if end_ts is not None:
            docs = [d for d in docs if d.get("last_ts", 0.0) <= end_ts]
        docs.sort(key=lambda d: d.get("last_ts", 0.0), reverse=True)
        total = len(docs)
        return [_jsonable(d) for d in docs[offset : offset + limit]], total

    async def turns(
        self, user_id: str, session_id: str, *, offset: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        """The verbatim turns of one of THIS user's conversations, in order."""
        docs = await self._docs.find(
            CONVERSATION_TURNS_COLLECTION,
            {"user_id": user_id, "session_id": session_id},
            limit=100000,
        )
        docs.sort(key=lambda d: d.get("turn_index", 0))
        return [_jsonable(d) for d in docs[offset : offset + limit]]


def _jsonable(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop the Mongo ``_id`` (an ObjectId isn't JSON-serializable) for the API."""
    return {k: v for k, v in doc.items() if k != "_id"}
