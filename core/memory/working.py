"""Working Memory (spec §4): the current session's turns, in process memory.

Per-session buffer of recent conversational turns for prompt assembly
(§10 step 3). Not persisted — ``close`` hands the full transcript to
Episodic Memory (§5) and Consolidation (§18), then clears the buffer.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    timestamp: str = Field(default_factory=_now_iso)
    emotion: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


class WorkingMemory:
    """Pure in-memory buffers; no I/O, so methods are synchronous."""

    def __init__(self) -> None:
        self._buffers: dict[str, list[Turn]] = {}

    def append(self, session_id: str, turn: Turn) -> None:
        self._buffers.setdefault(session_id, []).append(turn)

    def recent(self, session_id: str, n: int = 8) -> list[Turn]:
        """Last ``n`` turns in conversation order (rule 1)."""
        buffer = self._buffers.get(session_id, [])
        return list(buffer[-n:]) if n > 0 else []

    def all(self, session_id: str) -> list[Turn]:
        return list(self._buffers.get(session_id, []))

    def close(self, session_id: str) -> list[Turn]:
        """Return the full transcript and clear the buffer (rule 3)."""
        return self._buffers.pop(session_id, [])
