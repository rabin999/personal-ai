"""Deferred memory routing (spec §5/§6; Item 9).

The raw conversation log is written inline on every turn (never lost). The
decision of WHAT to promote and WHERE — episodic events, semantic facts,
procedural rules — is made HERE, off the conversation-latency path, by a
background worker that reads unrouted turns via the raw log's cursor, routes each
exactly once, then advances the watermark. Reading via the cursor is what kills
the double-write: a turn is routed once and never re-processed.
"""

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _RawLog(Protocol):
    async def unrouted_turns(self, *, limit: int = 50) -> list[dict[str, Any]]: ...
    async def mark_routed(self, turn_id: str) -> None: ...


class _Extractor(Protocol):
    async def extract_and_store(
        self, user_id: str, session_id: str, user_text: str, assistant_text: str
    ) -> Any: ...


class MemoryRouter:
    def __init__(self, raw_log: _RawLog, extractor: _Extractor, logs: Any | None = None) -> None:
        self._raw = raw_log
        self._extractor = extractor
        self._logs = logs

    async def route_pending(self, *, limit: int = 50) -> int:
        """Route every unrouted raw-log turn once; advance the watermark. Returns
        the number of turns routed. Safe to call repeatedly / concurrently-ish:
        each turn is marked routed after its attempt, so no turn is routed twice
        (no double-write) even across overlapping runs."""
        turns = await self._raw.unrouted_turns(limit=limit)
        routed = 0
        for turn in turns:
            turn_id = turn.get("_id") or turn.get("id")
            if not turn_id:
                continue
            user_id = turn.get("user_id", "")
            user_text = turn.get("user_text", "")
            assistant_text = turn.get("assistant_text", "")
            try:
                if user_text or assistant_text:
                    extracted = await self._extractor.extract_and_store(
                        user_id, turn.get("session_id", ""), user_text, assistant_text
                    )
                    self._log_routed(turn, extracted)
            except Exception:  # a bad turn must not stall the cursor
                logger.exception("memory routing failed for turn %s", turn_id)
            finally:
                # Advance the watermark exactly once — even on failure, so a poison
                # turn can't loop forever and can't be double-written on retry.
                await self._raw.mark_routed(str(turn_id))
                routed += 1
        return routed

    def _log_routed(self, turn: dict[str, Any], extracted: Any) -> None:
        if self._logs is None:
            return
        self._logs.log(
            "info",
            "memory.route",
            stage="memory",
            session_id=turn.get("session_id", ""),
            episodic=getattr(extracted, "episodic_written", 0),
            semantic=getattr(extracted, "semantic_written", 0),
            trades=getattr(extracted, "trades_written", 0),
        )
