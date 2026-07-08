"""Latency-tolerant sound classification (brief U10/U11/U12), one turn behind.

Mirrors the SER pattern (``voice/emotion.py``): the cough/register/ambient stage is
run in the background on the just-finished utterance and the NEXT turn reads whatever
completed — so it never sits on the live response path. ``current()`` never blocks.

Behind the ``SoundClassifier`` port, so the heuristic default swaps for a trained
CNN with one wiring line.
"""

import asyncio
import contextlib
import logging

from ports.sound import SoundClassifier, SoundRead

logger = logging.getLogger(__name__)


class LaggingSoundProvider:
    def __init__(self, classifier: SoundClassifier) -> None:
        self._classifier = classifier
        self._latest: SoundRead | None = None
        self._task: asyncio.Task[SoundRead | None] | None = None

    def schedule(self, audio_window: bytes, *, user_id: str, session_id: str | None = None) -> None:
        """Classify this utterance in the background (never awaited on the live path)."""
        self._absorb_if_done()
        self._task = asyncio.create_task(
            self._classifier.classify(audio_window, user_id=user_id, session_id=session_id)
        )

    def current(self) -> SoundRead | None:
        """The most recent completed read (previous turn's) — never blocks."""
        self._absorb_if_done()
        return self._latest

    def _absorb_if_done(self) -> None:
        if self._task is not None and self._task.done():
            try:
                self._latest = self._task.result()
            except Exception:
                logger.debug("sound classification task failed", exc_info=True)
                self._latest = None
            self._task = None

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(Exception):
                await self._task
