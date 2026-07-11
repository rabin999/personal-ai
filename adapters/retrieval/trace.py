"""Per-stage tracing for the verified-retrieval pipeline (CLAUDE.md §5: tracing is CORE).

Every stage of query → VerifiedResult emits a span carrying a brief human description,
its duration, and the key facts (how many links, which domains, corroboration count,
LLM cost). The spans fan out to whatever the composition root injects — the project's
``StructuredLogger`` (correlated by trace_id/session, non-blocking) and/or the durable
``TraceStore`` — and to NOTHING when neither is wired (the default no-op keeps the
pipeline runnable in the standalone harness). Tracing must NEVER break a turn: every
emit is guarded and swallowed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _StructuredLog(Protocol):
    def log(self, level: str, event: str, **fields: Any) -> None: ...


class _TraceStore(Protocol):
    async def record(self, user_id: str, event: dict[str, Any]) -> None: ...


class RetrievalTracer:
    """Fans retrieval spans to the app's structured logger and/or the durable trace store.

    Construct with either, both, or neither. ``span(...)`` is a context manager that times
    the block and records one event on exit; add result data to the yielded dict and it
    rides along. ``event(...)`` records a point-in-time note. Nothing here raises."""

    def __init__(
        self,
        *,
        logs: _StructuredLog | None = None,
        trace_store: _TraceStore | None = None,
        user_id: str = "",
        session_id: str | None = None,
    ) -> None:
        self._logs = logs
        self._store = trace_store
        self._user_id = user_id
        self._session_id = session_id
        self._pending: set[asyncio.Task[None]] = set()

    @contextmanager
    def span(self, stage: str, description: str, **data: Any) -> Iterator[dict[str, Any]]:
        extra: dict[str, Any] = {}
        started = time.perf_counter()
        level = "info"
        try:
            yield extra
        except Exception:
            level = "error"
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
            self._emit(stage, description, level, {**data, **extra, "duration_ms": duration_ms})

    def event(self, stage: str, description: str, level: str = "info", **data: Any) -> None:
        self._emit(stage, description, level, data)

    def _emit(self, stage: str, description: str, level: str, data: dict[str, Any]) -> None:
        # Structured app-log (sync, correlated, non-blocking).
        if self._logs is not None:
            try:
                self._logs.log(level, f"retrieval.{stage}", description=description, **data)
            except Exception:  # a log sink must never break retrieval
                logger.debug("retrieval trace log failed", exc_info=True)
        # Durable per-turn trace (async, fire-and-forget — never awaited on the turn path).
        if self._store is not None:
            try:
                task = asyncio.get_running_loop().create_task(
                    self._store.record(
                        self._user_id,
                        {
                            "session_id": self._session_id or "",
                            "stage": f"retrieval.{stage}",
                            "message": description,
                            "level": level,
                            "data": data,
                            "ts": time.time(),
                        },
                    )
                )
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
            except Exception:
                logger.debug("retrieval trace store failed", exc_info=True)


# A shared no-op so the pipeline can always call ``self._tracer`` unconditionally.
NOOP_TRACER = RetrievalTracer()
