"""Response feedback store (brief Part C — feedback loop).

Captures a user's thumbs up/down + optional note on a companion response, tied to
that response's trace (``session_id`` / ``turn_id`` / ``trace_id``) so a thumbs-down
can be inspected alongside the pipeline that produced it. Its own collection,
user-scoped (§0.5), queryable. Optional for the user — may or may not be given.
"""

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from ports.doc_store import DocStore

logger = logging.getLogger(__name__)

FEEDBACK_COLLECTION = "response_feedback"

Rating = Literal["up", "down"]


class Feedback(BaseModel):
    id: str
    user_id: str
    session_id: str
    turn_id: str | None = None
    trace_id: str | None = None
    rating: Rating
    note: str = ""
    created_at: str


class FeedbackStore:
    def __init__(self, docs: DocStore) -> None:
        self._docs = docs

    async def record(
        self,
        *,
        user_id: str,
        session_id: str,
        rating: Rating,
        turn_id: str | None = None,
        trace_id: str | None = None,
        note: str = "",
    ) -> Feedback:
        fb = Feedback(
            id=f"fb_{uuid.uuid4().hex[:12]}",
            user_id=user_id,
            session_id=session_id,
            turn_id=turn_id,
            trace_id=trace_id or session_id,
            rating=rating,
            note=note.strip()[:2000],
            created_at=datetime.now(UTC).isoformat(),
        )
        doc = fb.model_dump()
        doc["_id"] = doc.pop("id")
        doc["ts"] = time.time()
        await self._docs.put(FEEDBACK_COLLECTION, fb.id, doc)
        return fb

    async def list_for_user(
        self, user_id: str, *, offset: int = 0, limit: int = 50, rating: Rating | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        """This user's feedback, newest first, optionally filtered by rating."""
        query: dict[str, Any] = {"user_id": user_id}
        if rating is not None:
            query["rating"] = rating
        docs = await self._docs.find(FEEDBACK_COLLECTION, query, limit=100000)
        docs.sort(key=lambda d: d.get("ts", 0.0), reverse=True)
        return docs[offset : offset + limit], len(docs)
