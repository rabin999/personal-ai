"""Structured application logger (pluggable transport, brief Part B).

A tiny structured logger that fans one JSON-serializable record out to every
configured sink (file, stdout, …). Correlation ids (``trace_id`` / ``turn_id`` /
``user_id``) are bound per-turn via a context manager and automatically attached
to every record inside that scope, so a log line can be cross-referenced with the
per-turn trace store (``core/observability/trace_store.py``) and the cost ledger.

Depends only on the ``LogSink`` port — concrete sinks are wired at startup
(``core/`` never imports ``adapters/``). Sinks must not raise; this class also
guards each write so logging can never break a turn.
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from ports.log_sink import LogSink

logger = logging.getLogger(__name__)

# Correlation ids for the current turn (None outside a bound scope).
_CORRELATION: ContextVar[dict[str, str] | None] = ContextVar("log_correlation", default=None)

Level = str  # "debug" | "info" | "warning" | "error"


class StructuredLogger:
    def __init__(self, sinks: list[LogSink]) -> None:
        self._sinks = sinks

    @contextmanager
    def bind(
        self,
        *,
        trace_id: str | None = None,
        turn_id: str | int | None = None,
        user_id: str | None = None,
    ) -> Iterator[None]:
        """Attach correlation ids to every record emitted inside this scope."""
        current = dict(_CORRELATION.get() or {})
        if trace_id is not None:
            current["trace_id"] = str(trace_id)
        if turn_id is not None:
            current["turn_id"] = str(turn_id)
        if user_id is not None:
            current["user_id"] = str(user_id)
        token = _CORRELATION.set(current)
        try:
            yield
        finally:
            _CORRELATION.reset(token)

    def log(self, level: Level, event: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": time.time(),
            "level": level,
            "event": event,
            **(_CORRELATION.get() or {}),
            **fields,
        }
        for sink in self._sinks:
            try:
                sink.write(record)
            except Exception:  # a sink must never break the app
                logger.exception("log sink failed")

    def debug(self, event: str, **fields: Any) -> None:
        self.log("debug", event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self.log("info", event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self.log("warning", event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self.log("error", event, **fields)

    def close(self) -> None:
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:
                logger.exception("log sink close failed")
