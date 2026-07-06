"""Cost Ledger (spec §3): append-only spend records, queryable by any dimension.

``log`` is fire-and-forget — it schedules the write and returns immediately,
so ledger writes can never delay a user-facing response (rule 4). A failed
write is logged and swallowed; it must not break the conversation path.

Every query is ``user_id``-scoped (multi-tenant isolation invariant, §0.5).
"""

import asyncio
import logging
from typing import Any

from core.cost.models import Component, CostEntry, CostSummary
from ports.doc_store import DocStore

COST_COLLECTION = "cost_ledger"

logger = logging.getLogger(__name__)


class CostLedger:
    def __init__(self, docs: DocStore) -> None:
        self._docs = docs
        self._pending: set[asyncio.Task[None]] = set()

    def log(self, entry: CostEntry) -> None:
        """Schedule an append; returns immediately (rule 4: never blocks)."""
        task = asyncio.get_running_loop().create_task(self._write(entry))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def flush(self) -> None:
        """Await all scheduled writes (shutdown and tests)."""
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)

    async def _write(self, entry: CostEntry) -> None:
        try:
            await self._docs.insert(COST_COLLECTION, entry.model_dump())
        except Exception:
            logger.exception("cost ledger write failed (entry dropped): %s", entry.component)

    async def get(
        self,
        user_id: str,
        *,
        component: Component | None = None,
        provider: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        breakdown_by: str | None = None,
    ) -> CostSummary:
        """Total spend + entry count for one user, optionally broken down by a field."""
        match = self._match(
            user_id,
            component=component,
            provider=provider,
            project_id=project_id,
            session_id=session_id,
            since=since,
            until=until,
        )
        pipeline: list[dict[str, Any]] = [
            {"$match": match},
            {
                "$group": {
                    "_id": None,
                    "total_usd": {"$sum": "$cost_usd"},
                    "count": {"$sum": 1},
                }
            },
        ]
        rows = await self._docs.aggregate(COST_COLLECTION, pipeline)
        total = float(rows[0]["total_usd"]) if rows else 0.0
        count = int(rows[0]["count"]) if rows else 0

        breakdown: dict[str, float] | None = None
        if breakdown_by is not None:
            group_rows = await self._docs.aggregate(
                COST_COLLECTION,
                [
                    {"$match": match},
                    {
                        "$group": {
                            "_id": f"${breakdown_by}",
                            "total_usd": {"$sum": "$cost_usd"},
                        }
                    },
                ],
            )
            breakdown = {str(r["_id"]): float(r["total_usd"]) for r in group_rows}

        return CostSummary(total_usd=total, count=count, breakdown=breakdown)

    async def project_spend(
        self,
        user_id: str,
        project_id: str,
        since: str | None = None,
        until: str | None = None,
    ) -> float:
        """Total spend for one user's project over an optional ISO date range."""
        summary = await self.get(
            user_id, project_id=project_id, since=since, until=until
        )
        return summary.total_usd

    @staticmethod
    def _match(
        user_id: str,
        *,
        component: Component | None,
        provider: str | None,
        project_id: str | None,
        session_id: str | None,
        since: str | None,
        until: str | None,
    ) -> dict[str, Any]:
        match: dict[str, Any] = {"user_id": user_id}
        if component is not None:
            match["component"] = component
        if provider is not None:
            match["provider"] = provider
        if project_id is not None:
            match["metadata.project_id"] = project_id
        if session_id is not None:
            match["metadata.session_id"] = session_id
        if since is not None or until is not None:
            # ISO-8601 UTC strings sort lexicographically, so range filters
            # work directly on the string field.
            timestamp: dict[str, str] = {}
            if since is not None:
                timestamp["$gte"] = since
            if until is not None:
                timestamp["$lte"] = until
            match["timestamp"] = timestamp
        return match
