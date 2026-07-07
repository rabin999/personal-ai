"""Unit tests for multi-utterance accumulate/merge/split reasoning (A4)."""

from voice.multiutterance import classify_utterance, combine


def _d(prev, new, gap, responded=False):
    return classify_utterance(prev, new, gap, response_started=responded)


def test_incomplete_previous_then_quick_addition_accumulates() -> None:
    r = _d("I was thinking about the parser and", "maybe we rewrite it", 400)
    assert r.decision == "accumulate" and "incomplete" in r.reason


def test_quick_continuation_cue_merges() -> None:
    r = _d("let's cook pasta tonight.", "oh, and get some garlic bread too", 500)
    assert r.decision == "merge"
    r2 = _d("I finished the report.", "actually, wait — I didn't send it yet", 600)
    assert r2.decision == "merge"


def test_long_gap_is_a_separate_turn() -> None:
    r = _d("let's cook pasta tonight.", "and get garlic bread", 4000)
    assert r.decision == "split" and "gap" in r.reason


def test_unrelated_quick_statement_splits() -> None:
    r = _d("let's cook pasta tonight.", "what time is it in Tokyo", 500)
    assert r.decision == "split"


def test_response_already_started_is_split_barge_in() -> None:
    r = _d("tell me about the ocean.", "and volcanoes too", 300, responded=True)
    assert r.decision == "split" and "responding" in r.reason


def test_combine_joins_by_decision() -> None:
    assert combine("I like tea", "and coffee", "accumulate") == "I like tea and coffee"
    assert combine("I like tea", "oh and coffee", "merge") == "I like tea — oh and coffee"
    assert combine("I like tea", "what time is it", "split") == "what time is it"
