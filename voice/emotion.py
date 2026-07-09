"""Latency-tolerant SER orchestration (spec §22 rule 2): run one turn behind.

Acoustic emotion analysis is slow, so it must never sit on the live response
path. This provider kicks off analysis of the just-finished utterance in the
background and hands the *next* turn whatever read has completed so far —
``current()`` is always non-blocking. The first turn simply has no acoustic
read yet (returns None). When no acoustic read is available at all — the SER service is
optional and often unconfigured — the orchestrator supplies a TEXT-SENTIMENT read instead
(``core.reasoning.prosody.emotion_from_text``), so prosody is still driven per turn.

The read is a probabilistic signal, not ground truth (rule 4). Feeds Prompt
Assembly (§10 emotion signal) and the mood model (§17).
"""

import asyncio
import contextlib
import logging

from ports.ser import SER, EmotionRead

logger = logging.getLogger(__name__)


class LaggingEmotionProvider:
    def __init__(self, ser: SER) -> None:
        self._ser = ser
        self._latest: EmotionRead | None = None
        self._task: asyncio.Task[EmotionRead | None] | None = None

    def schedule(self, audio_window: bytes, *, user_id: str, session_id: str | None = None) -> None:
        """Start analysing this utterance in the background (never awaited here)."""
        self._absorb_if_done()
        self._task = asyncio.create_task(
            self._ser.analyze(audio_window, user_id=user_id, session_id=session_id)
        )

    def current(self) -> EmotionRead | None:
        """Most recent completed read (previous turn's) — never blocks (rule 2)."""
        self._absorb_if_done()
        return self._latest

    async def aclose(self) -> None:
        """Cancel any in-flight analysis on session teardown."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    def _absorb_if_done(self) -> None:
        if self._task is None or not self._task.done():
            return
        try:
            result = self._task.result()
        except asyncio.CancelledError:
            result = None
        except Exception:  # adapter already self-heals; never break the turn
            logger.exception("SER analysis task failed")
            result = None
        if result is not None:
            self._latest = result
        self._task = None
