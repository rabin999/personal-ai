"""Per-stage tracing for the verified-retrieval pipeline (design doc: tracing is CORE).

Every stage of query → VerifiedResult emits a span carrying a brief human description,
its duration, and the key facts (how many links, which domains, corroboration count,
LLM cost). The spans go out through the project's ``StructuredLogger`` (correlated by
trace_id/turn/user via its per-turn ``bind`` contextvar, non-blocking) — whose trace-store
sink persists each one against the CORRECT turn — and to NOTHING when it isn't wired (the
default no-op keeps the pipeline runnable in the standalone harness). We deliberately do NOT
also write the durable store directly: that second path had no turn id and orphaned a
duplicate of every span at turn 0. Tracing must NEVER break a turn: every emit is guarded.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _StructuredLog(Protocol):
    def log(self, level: str, event: str, **fields: Any) -> None: ...


class RetrievalTracer:
    """Fans retrieval spans to the app's structured logger (its trace-store sink persists
    them against the bound turn).

    Construct with a logger or neither. ``span(...)`` is a context manager that times the
    block and records one event on exit; add result data to the yielded dict and it rides
    along. ``event(...)`` records a point-in-time note. Nothing here raises."""

    def __init__(
        self,
        *,
        logs: _StructuredLog | None = None,
        user_id: str = "",
        session_id: str | None = None,
    ) -> None:
        self._logs = logs
        self._user_id = user_id
        self._session_id = session_id

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
        # ONE path: the structured logger (correlated, non-blocking). Its trace-store sink
        # reads the per-turn ``bind`` contextvar and persists this span against the CORRECT
        # turn — so we pass an explicit ``stage`` for the trace UI while keeping the
        # ``retrieval.{stage}`` event name + ``description`` field the log sinks expect.
        if self._logs is not None:
            try:
                self._logs.log(
                    level,
                    f"retrieval.{stage}",
                    stage=f"retrieval.{stage}",
                    description=description,
                    **data,
                )
            except Exception:  # a log sink must never break retrieval
                logger.debug("retrieval trace log failed", exc_info=True)


# A shared no-op so the pipeline can always call ``self._tracer`` unconditionally.
NOOP_TRACER = RetrievalTracer()
