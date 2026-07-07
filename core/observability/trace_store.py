"""Durable per-turn trace store (brief §1: full A→Z traceability).

Voice-turn trace events (``voice/trace.py``) already stream to the UI, but were
ephemeral. This store appends every event to a ``turn_traces`` collection so a
whole turn — VAD → STT → emotion → endpoint → assembly → routing → generation →
tools → response → TTS — is queryable after the fact for tuning and debugging.

Multi-tenant isolation (§0.5): every write carries ``user_id`` and every read is
``user_id``-scoped — one user's trace can never surface for another. Writes are
fire-and-forget from the runtime so persistence never blocks the conversation.

This deliberately reuses the existing Mongo stack instead of standing up a
separate tracing server (Langfuse); see ``docs/REMEDIATION_LOG.md`` for the
decision. The event shape is span-compatible, so an external exporter can be
layered on later without touching call sites.
"""

import logging
from typing import Any

from ports.doc_store import DocStore

logger = logging.getLogger(__name__)

TURN_TRACES_COLLECTION = "turn_traces"


class TraceStore:
    def __init__(self, docs: DocStore) -> None:
        self._docs = docs

    async def record(self, user_id: str, event: dict[str, Any]) -> None:
        """Append one trace event, user-scoped. Never raises into the caller."""
        try:
            await self._docs.insert(
                TURN_TRACES_COLLECTION,
                {
                    "user_id": user_id,
                    "session_id": event.get("session_id", ""),
                    "turn": int(event.get("turn", 0)),
                    "ts": float(event.get("ts", 0.0)),
                    "stage": event.get("stage", ""),
                    "message": event.get("message", ""),
                    "level": event.get("level", "info"),
                    "data": event.get("data", {}),
                },
            )
        except Exception:  # observability must never break the turn
            logger.exception("trace persistence failed")

    async def traces_for(
        self, user_id: str, session_id: str, *, limit: int = 2000
    ) -> list[dict[str, Any]]:
        """All persisted events for one of THIS user's sessions, turn-ordered."""
        docs = await self._docs.find(
            TURN_TRACES_COLLECTION,
            {"user_id": user_id, "session_id": session_id},
            limit=limit,
        )
        docs.sort(key=lambda d: (d.get("turn", 0), d.get("ts", 0.0)))
        return docs

    async def recent_sessions(self, user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Most-recent session ids for this user (for a trace-browser index)."""
        docs = await self._docs.find(TURN_TRACES_COLLECTION, {"user_id": user_id}, limit=10000)
        latest: dict[str, float] = {}
        for d in docs:
            sid = d.get("session_id", "")
            latest[sid] = max(latest.get(sid, 0.0), float(d.get("ts", 0.0)))
        ordered = sorted(latest.items(), key=lambda kv: kv[1], reverse=True)
        return [{"session_id": sid, "last_ts": ts} for sid, ts in ordered[:limit]]
