"""A 'what time/date is it in X?' question is answered from the DETERMINISTIC world clock in the
prompt, never a web search (whose summary mis-dates a cached page — the reported Nepal bug: reply
came back "Saturday, July 11" a day off). So such a query must NOT require a live lookup, even
when the LLM classifier flags needs_live_info — while a SCHEDULE question ("what time does the
market open?") is untouched.
"""

import pytest

from core.reasoning.localtime import is_time_of_day_query
from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import _requires_live_lookup


@pytest.mark.parametrize(
    "utterance",
    [
        "what time is it in Nepal right now?",
        "what's the time in nepal",
        "current time in kathmandu",
        "what time is it",
        "what is the current time",
        "what day is it today",
        "what's today's date",
        "the local time in london please",
    ],
)
def test_time_queries_detected(utterance: str) -> None:
    assert is_time_of_day_query(utterance)


@pytest.mark.parametrize(
    "utterance",
    [
        "what time does the market open",
        "what time is the meeting tomorrow",
        "what time should I leave for the airport",
        "who is the PM of Nepal",
        "what's the price of SYPNL",
        "should I bring an umbrella today",
        "how are you doing",
    ],
)
def test_non_time_queries_not_detected(utterance: str) -> None:
    assert not is_time_of_day_query(utterance)


def _prompt(utterance: str, needs_live: bool | None) -> AssembledPrompt:
    return AssembledPrompt(
        user_id="u_demo_001",
        session_id="s1",
        utterance=utterance,
        system_prompt="You are Companion.",
        messages=[{"role": "user", "content": utterance}],
        complexity_hint="simple",
        needs_live_info=needs_live,
    )


def test_time_query_never_requires_live_lookup_even_if_classifier_says_so() -> None:
    # The classifier commonly flags a time question as needing live info; the prompt clock
    # is authoritative, so we must NOT search (that's what mis-dated the reply).
    assert _requires_live_lookup(_prompt("what time is it in Nepal right now?", True)) is False
    assert _requires_live_lookup(_prompt("what's today's date?", True)) is False


def test_schedule_and_officeholder_still_search() -> None:
    # A schedule/officeholder question is NOT a clock reading — it still routes to a lookup.
    assert _requires_live_lookup(_prompt("what time does the NEPSE market open?", True)) is True
    assert _requires_live_lookup(_prompt("who is the current PM of Nepal?", None)) is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
