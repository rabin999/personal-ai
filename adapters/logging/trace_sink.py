"""Trace-store log sink — routes structured log records into the per-turn trace.

Bridges the structured logger (Part B) to the durable trace store (§1): a log
record carrying correlation ids (``trace_id`` = session, ``turn_id``, ``user_id``)
is written as a trace event so per-LLM-call spans (model, tokens, cost, latency)
show up in ``/debug/traces`` and the /traces UI, grouped by session — meeting the
"per-LLM-call token/cost/latency in the trace" bar (CLAUDE.md §5).

Records without a ``trace_id``/``user_id`` (logs outside a bound turn) are skipped
here — they still reach the file/stdout sinks. Writes are fire-and-forget so the
logger never blocks; failures never propagate.
"""

import asyncio
import contextlib
import logging
from typing import Any

from core.observability.trace_store import TraceStore

logger = logging.getLogger(__name__)


class TraceStoreLogSink:
    def __init__(self, store: TraceStore) -> None:
        self._store = store

    def write(self, record: dict[str, Any]) -> None:
        user_id = record.get("user_id")
        session = record.get("trace_id")
        if not user_id or not session:
            return  # not a per-turn record — file/stdout sinks still have it
        event = {
            "session_id": session,
            "turn": int(record.get("turn_id", 0) or 0),
            "ts": record.get("ts", 0.0),
            "stage": record.get("stage", "llm"),
            "message": record.get("event", ""),
            "level": record.get("level", "info"),
            "data": {
                k: v
                for k, v in record.items()
                if k not in ("user_id", "trace_id", "turn_id", "ts", "level", "event", "stage")
            },
        }
        with contextlib.suppress(RuntimeError):  # no running loop → skip trace persistence
            asyncio.get_running_loop().create_task(self._store.record(str(user_id), event))

    def close(self) -> None:
        pass
