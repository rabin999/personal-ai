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
    """Pure in-memory buffers; no I/O, so methods are synchronous.

    For endurance over very long sessions (F14) the live buffer is bounded: older
    turns are compacted into a per-session rolling ``summary`` (by the async
    SessionCompactor, off the reply path) and dropped from the buffer, so the prompt
    never grows unbounded. The raw turns are never lost — they stay in the durable
    conversation store (§6) and remain recallable (F3/F4). ``summary`` + the recent
    turns together keep the thread coherent for hours without ballooning tokens.
    """

    def __init__(self) -> None:
        self._buffers: dict[str, list[Turn]] = {}
        self._summaries: dict[str, str] = {}
        # Count of turns already folded into the summary + dropped (for compaction).
        self._compacted: dict[str, int] = {}

    def append(self, session_id: str, turn: Turn) -> None:
        self._buffers.setdefault(session_id, []).append(turn)

    def recent(self, session_id: str, n: int = 8) -> list[Turn]:
        """Last ``n`` turns in conversation order (rule 1)."""
        buffer = self._buffers.get(session_id, [])
        return list(buffer[-n:]) if n > 0 else []

    def all(self, session_id: str) -> list[Turn]:
        return list(self._buffers.get(session_id, []))

    def size(self, session_id: str) -> int:
        return len(self._buffers.get(session_id, []))

    # ── rolling summary / compaction (F14) ───────────────────────────────

    def summary(self, session_id: str) -> str:
        """The running summary of the earlier part of this session (may be empty)."""
        return self._summaries.get(session_id, "")

    def compact(self, session_id: str, *, keep_recent: int, summary: str) -> int:
        """Fold everything except the last ``keep_recent`` turns into ``summary`` and
        DROP them from the live buffer (they remain in the conversation store). The
        caller produced ``summary`` from those turns (+ the prior summary). Returns
        how many turns were dropped. Bounds the in-memory buffer + the prompt."""
        buffer = self._buffers.get(session_id, [])
        drop = max(0, len(buffer) - keep_recent)
        if drop <= 0:
            return 0
        self._buffers[session_id] = buffer[drop:]
        self._summaries[session_id] = summary
        self._compacted[session_id] = self._compacted.get(session_id, 0) + drop
        return drop

    def close(self, session_id: str) -> list[Turn]:
        """Return the full transcript and clear the buffer (rule 3)."""
        self._summaries.pop(session_id, None)
        self._compacted.pop(session_id, None)
        return self._buffers.pop(session_id, [])
