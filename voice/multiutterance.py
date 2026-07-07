"""Multi-utterance handling (addendum A4): are these successive sentences one
thought, an addition, or a new turn?

While the user is speaking they may endpoint one sentence, then quickly add
another before the companion has replied (e.g. say a thing, then remember a detail
and tack it on). Blindly concatenating everything is wrong; treating every
sentence as its own turn is also wrong. This module makes that decision from:
  - timing: how soon the next utterance arrived after the previous endpointed;
  - semantic continuity: does the new utterance continue/refine the previous, or
    start something unrelated;
  - state: had the companion already started responding? (then it's a barge-in /
    addition, reconciled by §24, not a fresh accumulation).

The decision is deterministic + explainable (logged in the trace), and the
semantic check is a cheap lexical heuristic so it never adds a paid call on the
hot path.
"""

import re
from typing import Literal

from pydantic import BaseModel

Decision = Literal["accumulate", "merge", "split"]

# A new utterance arriving within this gap of the previous endpoint is a candidate
# for the same thought (configurable via AudioPrefs later).
DEFAULT_CONTINUATION_GAP_MS = 1500.0

# Cues that the new utterance CONTINUES the previous one rather than starting fresh.
_CONTINUATION_STARTS = re.compile(
    r"^\s*(and|but|or|so|also|plus|actually|oh|wait|no|i mean|because|"
    r"cause|which|that|it|they|he|she|then|too|as well|by the way)\b",
    re.IGNORECASE,
)
# The previous utterance trailing off in a way that expects more.
_TRAILING_INCOMPLETE = re.compile(
    r"(,|\band\b|\bor\b|\bbut\b|\bbecause\b|\bso\b|…|-)\s*$", re.IGNORECASE
)


class UtteranceDecision(BaseModel):
    decision: Decision
    reason: str
    gap_ms: float


def classify_utterance(
    prev_text: str,
    new_text: str,
    gap_ms: float,
    *,
    response_started: bool,
    continuation_gap_ms: float = DEFAULT_CONTINUATION_GAP_MS,
) -> UtteranceDecision:
    """Decide how a NEW utterance relates to the immediately PREVIOUS one.

    - ``split``      → a separate new turn (long gap, or unrelated new content, or
      the companion already started replying — that's an addition/barge-in, handled
      by the interruption path, not accumulated here).
    - ``accumulate`` → one continuous thought: the previous trailed off incomplete
      and the new one arrived quickly — join them into a single turn.
    - ``merge``      → a connected addition/refinement arriving quickly (starts with
      'and/oh/actually/wait…' or a pronoun) — fold into the pending turn as extended
      context.
    """
    if response_started:
        return UtteranceDecision(
            decision="split",
            reason="companion already responding → addition/barge-in",
            gap_ms=gap_ms,
        )
    if gap_ms > continuation_gap_ms:
        return UtteranceDecision(
            decision="split",
            reason=f"gap {gap_ms:.0f}ms > {continuation_gap_ms:.0f}ms",
            gap_ms=gap_ms,
        )
    if _TRAILING_INCOMPLETE.search(prev_text.strip()):
        return UtteranceDecision(
            decision="accumulate", reason="previous thought trailed off incomplete", gap_ms=gap_ms
        )
    if _CONTINUATION_STARTS.match(new_text):
        return UtteranceDecision(
            decision="merge",
            reason="new utterance continues the previous (cue word)",
            gap_ms=gap_ms,
        )
    return UtteranceDecision(
        decision="split", reason="quick but semantically a new statement", gap_ms=gap_ms
    )


def combine(prev_text: str, new_text: str, decision: Decision) -> str:
    """Join two utterances per the decision (accumulate/merge → one text)."""
    if decision == "split":
        return new_text
    sep = " " if decision == "accumulate" else " — "
    return f"{prev_text.strip()}{sep}{new_text.strip()}"
