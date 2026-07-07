"""Durable tool-result store (spec §13; brief §5.2).

Every tool call's result — especially web_search / news — is persisted here,
keyed by user + timestamp + tool + query, so the user can later ask "what was
that news?" / "what did that search find?" and it resolves against real stored
output instead of the model hallucinating.

Multi-tenant isolation (§0.5): every write carries ``user_id`` and every read is
``user_id``-scoped. Writes are best-effort and never raise into the tool path.
"""

import logging
import time
from datetime import UTC, datetime
from typing import Any

from ports.doc_store import DocStore

logger = logging.getLogger(__name__)

TOOL_RESULTS_COLLECTION = "tool_results"


def _query_of(args: dict[str, Any]) -> str:
    """Best-effort human-readable query label for the call."""
    for key in ("query", "q", "ticker", "name"):
        if args.get(key):
            return str(args[key])
    return ""


class ToolResultStore:
    def __init__(self, docs: DocStore) -> None:
        self._docs = docs

    async def record(
        self,
        *,
        user_id: str,
        session_id: str | None,
        tool_id: str,
        args: dict[str, Any],
        output: dict[str, Any],
    ) -> None:
        try:
            await self._docs.insert(
                TOOL_RESULTS_COLLECTION,
                {
                    "user_id": user_id,
                    "session_id": session_id or "",
                    "tool": tool_id,
                    "query": _query_of(args),
                    "args": args,
                    "output": output,
                    "ts": time.time(),
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
        except Exception:  # persistence is best-effort; never break the tool call
            logger.exception("tool-result persistence failed for %s", tool_id)

    async def latest(
        self, user_id: str, *, tool: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """This user's most recent tool results (optionally for one tool), newest first."""
        query: dict[str, Any] = {"user_id": user_id}
        if tool:
            query["tool"] = tool
        docs = await self._docs.find(TOOL_RESULTS_COLLECTION, query, limit=1000)
        docs.sort(key=lambda d: d.get("ts", 0.0), reverse=True)
        return docs[:limit]
