"""Barge-in & Interruption (spec §24): stop output cleanly, protect writes.

On user speech during playback: stop TTS immediately and cancel the turn's
in-flight generation. If an action-tool write is mid-execution the interrupt
is deferred until the write completes (§13 already shields the write itself;
this controller sequences the interrupt after it). AEC must be on for this
to be safe — validated by §19's config check.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


class BargeInController:
    def __init__(self) -> None:
        self._tts_task: asyncio.Task[None] | None = None
        self._generation_task: asyncio.Task[object] | None = None
        self._writes_in_flight = 0
        self._writes_done = asyncio.Event()
        self._writes_done.set()
        self.interrupts_handled = 0

    # ── registration by the session runtime ─────────────────────────────

    def attach_tts(self, task: "asyncio.Task[None]") -> None:
        self._tts_task = task

    def attach_generation(self, task: "asyncio.Task[object]") -> None:
        self._generation_task = task

    @contextlib.asynccontextmanager
    async def protected_write(self) -> AsyncIterator[None]:
        """Wrap action-tool writes: interrupts wait for the write (rule 3)."""
        self._writes_in_flight += 1
        self._writes_done.clear()
        try:
            yield
        finally:
            self._writes_in_flight -= 1
            if self._writes_in_flight == 0:
                self._writes_done.set()

    # ── the interrupt path ───────────────────────────────────────────────

    async def on_user_speech(self, session_id: str) -> None:
        """VAD detected user speech during TTS playback (§19 gate event)."""
        if self._writes_in_flight:
            logger.info("barge-in during action write: deferring until write completes")
            await self._writes_done.wait()  # write finishes untouched
        self._stop_output()
        self.interrupts_handled += 1

    def _stop_output(self) -> None:
        if self._tts_task is not None and not self._tts_task.done():
            self._tts_task.cancel()  # §23 stream closes mid-chunk
        if self._generation_task is not None and not self._generation_task.done():
            self._generation_task.cancel()  # §11 call abandoned
        self._tts_task = None
        self._generation_task = None
