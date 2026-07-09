"""E2 — the volatility classifier, measured on labeled data instead of three examples.

`is_volatile_question` and `_is_live_info_query` are binary classifiers whose false
negatives make the app factually wrong: the turn never reaches a tool, and a stale
training-data answer is spoken as current fact. Proven in `SESSION_REPORT_F1-F6` §F5 —
`"LTP of SYPNL"` → 0 searches, `"price of SYPNL"` → 1 search → correct answer. Same
fixture; phrasing alone decided. Neither had ever been measured.

This file measures them over `tests/labeled/volatility.jsonl` (174 questions, 87
volatile / 87 stable, 22 classes) and pins the result. It covers the DETERMINISTIC half
only — the primary gate is `needs_live_info`, produced by a real LLM in the
orchestrator's `context_intent` node, and is measured by `scripts/measure_classifiers.py
--real` (its numbers land in `docs/ENGINE_QUALITY_GATE.md`).

The pins below are characterisation, not aspiration: the deterministic backstop's recall
is 0.839, BELOW the 0.95 bar in the brief. That gap is recorded as a product defect
(`docs/DEFECTS_FOUND.md` D-1) and deliberately not fixed here. Pinning the exact
false-negative set means any change — a fix or a regression — is visible immediately
rather than silently moving a percentage.
"""

import json
from pathlib import Path

import pytest

from core.reasoning.response_gen import _is_live_info_query
from core.reasoning.volatility import is_volatile_question

DATA = Path(__file__).parents[1] / "labeled" / "volatility.jsonl"
ROWS = [json.loads(line) for line in DATA.read_text().splitlines() if line.strip()]
VOLATILE = [r for r in ROWS if r["label"] == "volatile"]
STABLE = [r for r in ROWS if r["label"] == "stable"]


def _gate(q: str) -> bool:
    """Exactly what `_requires_live_lookup` falls back to when the LLM verdict is
    unusable (`None`) or False — i.e. the whole gate on the text path's simple turns."""
    return is_volatile_question(q) or _is_live_info_query(q)


# The complete false-negative set of the deterministic gate, at the commit that first
# measured it. Each of these is a question whose true answer changes over time and which
# the backstop alone would answer from training data. Recorded in DEFECTS_FOUND.md D-1.
KNOWN_FALSE_NEGATIVES = {
    "who's leading the polls?",
    "who manages Manchester United now?",
    "LTP of OP",
    "what's gold going for?",
    "give me a quote on NABIL",
    "what did the NEPSE index close at?",
    "how cold is it in London?",
    "is it snowing in the mountains?",
    "do I need a jacket?",
    "where is Arsenal in the table?",
    "did Nepal win?",
    "what's the newest iPhone?",
    "how many users does it have now?",
    "my portfolio is stressing me out, how's OP doing",
}

# The stable questions the deterministic gate would needlessly search. Over-searching is
# its own failure: it costs a second of latency and derails a conversational turn.
KNOWN_FALSE_POSITIVES = {"is it worth learning Rust?"}


def test_the_labeled_set_is_balanced_and_unique() -> None:
    """A skewed or duplicated set produces a flattering number, not a measurement."""
    assert len(ROWS) >= 150, f"the brief requires >= 150 labeled questions, got {len(ROWS)}"
    assert len(VOLATILE) == len(STABLE) == 87
    assert len({r["q"] for r in ROWS}) == len(ROWS), "duplicate questions inflate the score"


def test_deterministic_gate_false_negatives_are_exactly_the_known_set() -> None:
    """Recall, stated as the exact list rather than a percentage. A fix shrinks this set;
    a regression grows it. Either way the diff names the questions that broke."""
    actual = {r["q"] for r in VOLATILE if not _gate(r["q"])}
    assert actual == KNOWN_FALSE_NEGATIVES, (
        "the deterministic volatility gate changed which questions it misses.\n"
        f"  newly missed (REGRESSION — these now get a stale answer): "
        f"{sorted(actual - KNOWN_FALSE_NEGATIVES)}\n"
        f"  newly caught (FIX — update KNOWN_FALSE_NEGATIVES): "
        f"{sorted(KNOWN_FALSE_NEGATIVES - actual)}"
    )


def test_deterministic_gate_false_positives_are_exactly_the_known_set() -> None:
    actual = {r["q"] for r in STABLE if _gate(r["q"])}
    assert actual == KNOWN_FALSE_POSITIVES, (
        f"the gate's needless searches changed.\n"
        f"  newly over-searching: {sorted(actual - KNOWN_FALSE_POSITIVES)}\n"
        f"  newly restrained: {sorted(KNOWN_FALSE_POSITIVES - actual)}"
    )


def test_deterministic_gate_recall_does_not_regress() -> None:
    """The measured floor, so a change that keeps the FN *count* but swaps *which*
    questions is still caught by the test above, and a pure regression is caught here."""
    recall = sum(_gate(r["q"]) for r in VOLATILE) / len(VOLATILE)
    assert recall == pytest.approx(0.839, abs=0.01), f"recall moved: {recall:.3f}"
    assert recall < 0.95, (
        "the deterministic gate now clears the 0.95 bar — DEFECTS_FOUND.md D-1 is fixed; "
        "delete this assertion and raise the floor"
    )


@pytest.mark.parametrize(
    "q",
    [
        "who is the current prime minister of Nepal?",
        "is Tim Cook still the CEO of Apple?",
        "what's the current LTP of OP?",
        "what's the weather in Kathmandu right now?",
        "what's the exchange rate today?",
    ],
    ids=lambda q: q[:36],
)
def test_the_headline_volatile_questions_are_always_caught(q: str) -> None:
    """These are the failures the user actually reported. Whatever else drifts, the
    deterministic backstop must never stop catching these — no LLM verdict required."""
    assert _gate(q), f"{q!r} would be answered from stale training data"


@pytest.mark.parametrize(
    "q",
    [
        "what's 15% of 240?",
        "I'm feeling low today",
        "do you actually care about me?",
        "when do I take my meds?",
        "hi",
    ],
    ids=lambda q: q[:36],
)
def test_the_headline_stable_turns_never_trigger_a_search(q: str) -> None:
    assert not _gate(q), f"{q!r} would trigger a needless web search"


def test_topic_regex_alone_is_not_a_volatility_classifier() -> None:
    """`_is_live_info_query` was the sole routing gate before S1. It scores recall 0.391
    on this set: it misses EVERY role-holder question and every "still" question. This
    pins why it may never again be used alone."""
    recall = sum(_is_live_info_query(r["q"]) for r in VOLATILE) / len(VOLATILE)
    assert recall < 0.5, f"unexpectedly high: {recall:.3f}"
    assert not _is_live_info_query("who is the current prime minister of Nepal?")
    assert not _is_live_info_query("is Tim Cook still the CEO of Apple?")
