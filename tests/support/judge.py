"""Reusable LLM-as-judge for the real-call suites (plan §3/§4).

Single source of truth now lives in ``core.eval.judge`` so the LIVE per-turn
evaluator (design: response-quality eval) and these test suites share one calibrated rubric —
no drift. Re-exported here to keep the existing test imports working.
"""

from __future__ import annotations

from core.eval.judge import (
    COMPANION_VOICE_RUBRIC,
    JUDGE_TIER,
    Verdict,
    judge_companion_voice,
)

__all__ = ["COMPANION_VOICE_RUBRIC", "JUDGE_TIER", "Verdict", "judge_companion_voice"]
