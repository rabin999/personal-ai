"""Deterministic volatility backstop (S1).

Whether a turn needs a live lookup is decided by the REASONING step
(`AssembledPrompt.needs_live_info`, produced by the orchestrator's context/intent node).
This module is the **backstop**, not the primary mechanism.

It exists because that classifier is measurably unreliable — not in its judgement (4-5 of 5
correct on the probe set) but in its delivery: the provider returns a bare ``"{"`` with
``output_tokens=0`` on roughly 1 call in 6, across every combination of temperature,
``max_tokens`` and reasoning-budget we tried. A `JSONDecodeError` used to be swallowed
straight into ``needs_live_info=False``, i.e. "answer from training data". That is how
"who is the president of the United States?" came back as *"Joe Biden is still the
President"* in July 2026, with zero searches.

So: **a classifier failure must never mean "don't search".**

This is deliberately NOT another topic-keyword list (the existing
`_LIVE_INFO_QUERY` regex is that, and it returns False for "who is the current prime
minister of Nepal?"). It matches the *shape of a question whose true answer changes over
time*:

- a role-holder question ("who is the CEO of X", "who's the prime minister")
- a "still" question ("is Tim Cook still the CEO?")
- temporal deixis on a factual question ("current", "latest", "right now", "today")
- market/score vocabulary ("price", "LTP", "last traded", "score", "who won")

and it refuses to fire on questions aimed at the companion itself ("how are you doing
today?", "do you actually care about me?") unless they carry an external topic marker
("can you check the price of SYPNL?").

Over-searching is its own failure, so the controls matter as much as the positives:
"what's 15% of 240?" and "I'm feeling low today" must never trip this.
"""

from __future__ import annotations

import re

# Looks like a question at all. Without this, "I'm feeling low today" (temporal deixis!)
# would trip the volatility check.
_INTERROGATIVE = re.compile(
    r"^\s*(who|what|whats|what's|when|where|which|how|why|is|are|was|were|do|does|did|"
    r"has|have|had|can|could|will|would|should|any)\b",
    re.IGNORECASE,
)

# Titles/roles whose holder changes. Kept to ROLES, not topics.
_ROLE = (
    r"president|prime minister|\bpm\b|chancellor|premier|ceo|cfo|cto|chair(?:man|person)?|"
    r"director|leader|head of|king|queen|emperor|pope|mayor|governor|senator|minister|"
    r"manager|coach|captain|champion|champions|title ?holder|winner|owner"
)
_ROLE_HOLDER = re.compile(rf"\bwho(?:'s| is| are| was| were)?\b[^?]*\b(?:{_ROLE})\b", re.IGNORECASE)
_ROLE_OF = re.compile(rf"\b(?:the\s+)?(?:current\s+)?(?:{_ROLE})\s+of\b", re.IGNORECASE)

# "is X still the Y" / "does X still ..." — the answer was true once and may not be now.
_STILL = re.compile(r"\bstill\b", re.IGNORECASE)

# "as of now" deixis. Only meaningful on a factual question (see `_INTERROGATIVE`).
_DEIXIS = re.compile(
    r"\b(current(ly)?|latest|most recent|right now|at the moment|these days|nowadays|"
    r"as of (now|today)|so far this|today|tonight|this (week|month|year|season)|up to date)\b",
    re.IGNORECASE,
)

# Market / scoreboard vocabulary — values that move.
_MOVING_VALUE = re.compile(
    r"\b(price|prices|ltp|last traded|last trade|quote|share price|stock price|exchange rate|"
    r"rate|worth|valuation|market cap|trading at|score|scores|standings|league table|"
    r"who won|winner|result|results)\b",
    re.IGNORECASE,
)

# Questions aimed at the companion, not at the world.
_SELF_DIRECTED = re.compile(r"\b(you|your|yourself|yours)\b", re.IGNORECASE)
# ... unless they carry an external topic marker, so "can you check the price of SYPNL?"
# is still a live-info question.
_EXTERNAL_MARKER = re.compile(
    r"\b(price|ltp|weather|news|score|stock|share|rate|traded|headline|forecast|"
    r"president|prime minister|ceo|minister)\b",
    re.IGNORECASE,
)


def is_volatile_question(utterance: str) -> bool:
    """True when the question's true answer can change over time, so a training-data
    answer risks being stale. Deliberately conservative on self-directed questions."""
    text = (utterance or "").strip()
    if not text:
        return False
    if "?" not in text and not _INTERROGATIVE.match(text):
        return False  # a statement, not a question ("I'm feeling low today")
    # An OPINION wrapper around a volatile fact is still volatile (bucket D): "what do YOU
    # think about the current PM of Nepal?" must verify who the PM is first. So the
    # self-directed exclusion ("how are you doing?") only fires when there's NO external
    # volatile signal at all — checked against the real role/value detectors, not a narrower
    # marker list that missed abbreviations like "PM".
    external = bool(
        _ROLE_HOLDER.search(text) or _ROLE_OF.search(text) or _MOVING_VALUE.search(text)
    )
    if _SELF_DIRECTED.search(text) and not external and not _EXTERNAL_MARKER.search(text):
        return False  # "how are you doing today?", "do you actually care about me?"
    return bool(external or _STILL.search(text) or _DEIXIS.search(text))
