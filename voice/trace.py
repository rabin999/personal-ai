"""Session trace events — the start-to-finish record of a voice turn.

Every stage of the runtime (§19 VAD gate → §20 STT → §21 endpointing → §10
assembly → §11/§12 generation → §23 TTS) emits a ``TraceEvent``. The API
streams these to the UI's log sidebar so a human can watch exactly what the
companion is doing and why. Trace is observability only — it never carries
another user's data and never blocks the turn (fire-and-forget over a queue).
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, Field

Stage = Literal[
    "session",
    "vad",
    "stt",
    "endpoint",
    "emotion",
    "assembly",
    "retrieval",
    "router",
    "generation",
    "reflection",
    "memory",
    "response",
    "tts",
    "barge_in",
    "audio",  # U10-U12 sound-awareness (health check-in, tone mirror, surroundings)
    "error",
]
Level = Literal["info", "debug", "warn", "error"]


class TraceEvent(BaseModel):
    session_id: str
    turn: int = 0  # groups events into per-utterance conversation turns (UI collapse)
    ts: float = Field(default_factory=time.time)
    stage: Stage
    message: str
    level: Level = "info"
    data: dict[str, Any] = Field(default_factory=dict)


class TraceEmitter:
    """Async fan-out of trace events to a single consumer (the WS connection)."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._turn = 0
        self._queue: asyncio.Queue[TraceEvent | None] = asyncio.Queue()

    def begin_turn(self) -> int:
        """Start a new conversation turn; subsequent events group under it."""
        self._turn += 1
        return self._turn

    @property
    def current_turn(self) -> int:
        """The trace turn index events are currently grouped under (§6 cross-ref)."""
        return self._turn

    def emit(
        self,
        stage: Stage,
        message: str,
        *,
        level: Level = "info",
        **data: Any,
    ) -> None:
        """Record an event; returns immediately (never blocks the turn)."""
        self._queue.put_nowait(
            TraceEvent(
                session_id=self._session_id,
                turn=self._turn,
                stage=stage,
                message=message,
                level=level,
                data=data,
            )
        )

    def close(self) -> None:
        """Signal the consumer that no more events are coming."""
        self._queue.put_nowait(None)

    async def events(self) -> AsyncIterator[TraceEvent]:
        """Yield events until ``close`` is called."""
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event
