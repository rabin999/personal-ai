"""S1 — the deterministic volatility backstop.

Routing used to hang off `_is_live_info_query`, a topic-keyword regex that returns False
for "who is the current prime minister of Nepal?". The turn then took the non-agentic
streaming path, could never reach a tool, and answered from training data ("Joe Biden is
still the President", in July 2026).

The reasoning step now decides. This module is the backstop for when it can't: measured,
~1 in 6 `context_intent` calls returns a bare `"{"` that used to be swallowed into
"don't search".

Over-searching is its own failure, so the CONTROLS matter as much as the positives.
"""

import pytest

from core.reasoning.volatility import is_volatile_question

VOLATILE = [
    "who is the current prime minister of Nepal?",
    "who is the president of the United States?",
    "what's the LTP of SYPNL?",
    "what's the price of SYPNL?",
    "what's the weather in Kathmandu right now?",
    "is Tim Cook still the CEO of Apple?",
    "what happened in the news today?",
    "who won the world cup?",
    "can you check the price of SYPNL?",  # self-directed BUT carries an external marker
    "what's the exchange rate today?",
]

STABLE = [
    "what's 15% of 240?",  # arithmetic
    "I'm feeling low today",  # a statement with temporal deixis — not a question
    "what's the capital of France?",  # stable fact
    "who is Nikola Tesla?",  # historical, no role
    "hi",
    "thanks, that actually helps",
    "do you actually care about me?",  # about the companion, not the world
    "how are you doing today?",  # ditto, despite "today"
    "is that going to be a problem?",
    "when do I take my meds?",  # the user's own memory, not the web
    "I got the promotion!!",
]


@pytest.mark.parametrize("q", VOLATILE)
def test_volatile_questions_are_flagged(q: str) -> None:
    assert is_volatile_question(q), f"{q!r} would be answered from stale training data"


@pytest.mark.parametrize("q", STABLE)
def test_stable_and_social_turns_do_not_trigger_a_search(q: str) -> None:
    assert not is_volatile_question(q), f"{q!r} would trigger a needless web search"


def test_a_statement_is_never_volatile_even_with_temporal_words() -> None:
    assert not is_volatile_question("I'm feeling low today")
    assert not is_volatile_question("the current situation at work is rough")
