"""The companion must never name itself. set_companion_name only accepts a name the
user actually said (fix for the hallucinated "Norsylinder" companion name). Verifies
the utterance-matching guard used by the tool handler."""

import pytest

from core.tools.builtin.core_tools import _name_came_from_user, _strip_unrequested_date


@pytest.mark.parametrize(
    ("name", "utterance", "accepted"),
    [
        ("Saathi", "you can call you Saathi from now on", True),
        ("Saathi", "i want to call you saathi", True),
        ("Sunny", "call you Sunny", True),
        ("Sunny Boy", "i'll call you Sunny Boy", True),
        # accents/case fold
        ("Sofía", "your name is sofia", True),
        # the bug: a name the user never uttered
        ("Norsylinder", "what should I call you?", False),
        ("Norsylinder", "hey, how are you today", False),
        # a partial match is not enough
        ("Sunny Boy", "i'll call you Sunny", False),
        # unknown utterance (non-conversational caller) stays lenient
        ("Buddy", "", True),
    ],
)
def test_name_must_come_from_user(name: str, utterance: str, accepted: bool) -> None:
    assert _name_came_from_user(name, utterance) is accepted


def test_strips_model_appended_date_but_keeps_user_requested_one() -> None:
    """A live-search query must not carry a date the model appended that the user never asked
    for — a stale year ('2024') pins it to old data, and even the CURRENT month ('July 2026')
    is narrower/worse than letting 'current/latest' carry the recency. A date the user named
    is kept."""
    # model bolted a stale year onto a "current" question the user never dated
    assert (
        _strip_unrequested_date("current PM of Nepal 2024", "who is the current PM of Nepal?")
        == "current PM of Nepal"
    )
    # the user's actual complaint: the CURRENT month/year appended → dropped, rely on "current"
    assert (
        _strip_unrequested_date(
            "current prime minister of Nepal July 2026", "who is the PM of Nepal?"
        )
        == "current prime minister of Nepal"
    )
    # a bare current year the model tacked on → dropped
    assert (
        _strip_unrequested_date("latest NEPSE index 2026", "latest NEPSE index")
        == "latest NEPSE index"
    )
    # user explicitly asked about a specific year → keep it
    assert _strip_unrequested_date("who won in 2019", "who won in 2019?") == "who won in 2019"
