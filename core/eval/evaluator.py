"""Per-turn response-quality evaluator (CLAUDE.md §6/§7).

Runs the companion-voice LLM-as-judge on a completed turn OFF the reply path
(fire-and-forget) and posts its verdict to the eval backend (Langfuse) as scores
on the SAME (session, turn) trace the reply produced — so quality is inspectable
right next to the pipeline, and the automated judge and human thumbs feedback
calibrate each other on one trace.

Gated by config (an extra judge LLM call per turn costs money): off by default,
on via ``settings.langfuse_eval_enabled``. Never blocks or breaks a turn.
"""

from __future__ import annotations

import asyncio
import logging

from core.eval.judge import judge_companion_voice
from ports.llm import LLM
from ports.score_sink import ScoreSink

logger = logging.getLogger(__name__)

# Score names as they appear in Langfuse. companion_voice is the 1-5 quality; the
# chatbot_like flag is the hard-fail signal (1.0 = sounded like an assistant).
SCORE_QUALITY = "companion_voice"
SCORE_CHATBOT = "chatbot_like"


class TurnEvaluator:
    def __init__(
        self,
        llm: LLM,
        scores: ScoreSink | None,
        *,
        enabled: bool = False,
    ) -> None:
        self._llm = llm
        self._scores = scores
        # Only run when explicitly enabled AND there's a backend to post to.
        self._enabled = enabled and scores is not None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def schedule(self, *, session_id: str, turn: int, user_msg: str, reply: str) -> None:
        """Fire the evaluation as a background task — never on the reply path."""
        if not self._enabled or not reply.strip():
            return
        task = asyncio.create_task(self._evaluate(session_id, turn, user_msg, reply))
        task.add_done_callback(lambda t: t.exception())

    async def _evaluate(self, session_id: str, turn: int, user_msg: str, reply: str) -> None:
        try:
            verdict = await judge_companion_voice(self._llm, user_msg, reply)
        except Exception:
            logger.debug("turn evaluation (judge) failed", exc_info=True)
            return
        assert self._scores is not None  # guarded by _enabled
        # Best-effort scoring; ScoreSink.score never raises, but guard anyway.
        try:
            self._scores.score(
                session_id=session_id,
                turn=turn,
                name=SCORE_QUALITY,
                value=float(verdict.companion_score),
                comment=verdict.reason,
            )
            self._scores.score(
                session_id=session_id,
                turn=turn,
                name=SCORE_CHATBOT,
                value=1.0 if verdict.chatbot_like else 0.0,
                comment=verdict.reason,
            )
        except Exception:
            logger.debug("turn evaluation (score submit) failed", exc_info=True)
