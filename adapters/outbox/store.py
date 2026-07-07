"""Transactional outbox store (brief §4).

The welcome email must never fail or delay signup, so signup writes a *pending*
``outbox`` record in the SAME operation that creates the user; a background
worker delivers it later with retry + backoff. Idempotent on the outbox ``_id``
— no loss, no double-send. Statuses: ``pending`` → ``sent`` | ``failed`` (after
capped attempts) | ``skipped`` (delivery disabled, e.g. no SMTP configured).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from ports.doc_store import DocStore

OUTBOX_COLLECTION = "outbox"
WELCOME_EMAIL = "welcome_email"
MAX_ATTEMPTS = 5
# Exponential-ish backoff between attempts (seconds), indexed by attempt count.
_BACKOFF_S = (0, 30, 120, 600, 3600)


class OutboxRecord(BaseModel):
    id: str
    type: str
    payload: dict[str, Any]
    status: str  # pending | sent | failed | skipped
    attempts: int = 0
    created_at: str
    updated_at: str
    next_attempt_at: str
    last_error: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


class OutboxStore:
    def __init__(self, docs: DocStore) -> None:
        self._docs = docs

    async def enqueue_welcome(self, user_id: str, email: str, name: str | None) -> str:
        """Write a pending welcome-email record. Called in the signup operation so
        signup never blocks on SMTP (brief §4)."""
        oid = "ob_" + uuid.uuid4().hex[:16]
        now = _now()
        doc = {
            "_id": oid,
            "type": WELCOME_EMAIL,
            "payload": {"user_id": user_id, "email": email, "name": name},
            "status": "pending",
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
            "next_attempt_at": now,
        }
        await self._docs.put(OUTBOX_COLLECTION, oid, doc)
        return oid

    async def due(self, limit: int = 20) -> list[OutboxRecord]:
        """Pending records whose backoff window has elapsed (DocStore find is
        equality-only, so the time gate is applied in-process)."""
        rows = await self._docs.find(OUTBOX_COLLECTION, {"status": "pending"}, limit=limit)
        now = datetime.now(UTC)
        due: list[OutboxRecord] = []
        for row in rows:
            rec = OutboxRecord.model_validate({"id": row["_id"], **_without_id(row)})
            if datetime.fromisoformat(rec.next_attempt_at) <= now:
                due.append(rec)
        return due

    async def mark_sent(self, oid: str) -> None:
        await self._patch(oid, {"status": "sent", "updated_at": _now(), "last_error": None})

    async def mark_skipped(self, oid: str, reason: str) -> None:
        await self._patch(oid, {"status": "skipped", "updated_at": _now(), "last_error": reason})

    async def mark_attempt_failed(self, oid: str, error: str) -> None:
        """Record a failed attempt: retry (stay pending, schedule next) until the
        attempt cap, then mark failed so it stops looping."""
        doc = await self._docs.get(OUTBOX_COLLECTION, oid)
        if doc is None:
            return
        attempts = int(doc.get("attempts", 0)) + 1
        if attempts >= MAX_ATTEMPTS:
            patch = {"status": "failed", "attempts": attempts, "last_error": error}
        else:
            delay = _BACKOFF_S[min(attempts, len(_BACKOFF_S) - 1)]
            next_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
            patch = {"attempts": attempts, "last_error": error, "next_attempt_at": next_at}
        patch["updated_at"] = _now()
        await self._patch(oid, patch)

    async def _patch(self, oid: str, patch: dict[str, Any]) -> None:
        doc = await self._docs.get(OUTBOX_COLLECTION, oid)
        if doc is None:
            return
        doc.update(patch)
        await self._docs.put(OUTBOX_COLLECTION, oid, doc)


def _without_id(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k != "_id"}
