"""Port: a structured-log sink (pluggable logging transport, brief Part B).

Application logs are structured records (dicts) fanned out to one or more sinks
chosen by config — file by default, optionally stdout or, later, a remote sink —
without changing call sites. ``core/`` depends only on this interface; concrete
sinks live in ``adapters/logging/`` and are wired at startup.

Each record carries correlation ids (``trace_id`` / ``turn_id`` / ``user_id``) so a
log line ties back to the per-turn trace (``core/observability``).
"""

from typing import Any, Protocol


class LogSink(Protocol):
    def write(self, record: dict[str, Any]) -> None:
        """Emit one structured record. Must not raise — logging never breaks a turn."""
        ...

    def close(self) -> None:
        """Flush/close any resources (files). Idempotent."""
        ...
