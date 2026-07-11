"""The companion must never name itself. set_companion_name only accepts a name the
user actually said (fix for the hallucinated "Norsylinder" companion name). Verifies
the utterance-matching guard used by the tool handler."""

from datetime import UTC, datetime

import pytest

from core.tools.builtin.core_tools import _name_came_from_user, _strip_unrequested_stale_year


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


def test_strips_model_invented_stale_year_but_keeps_user_requested_one() -> None:
    """A live-search query must not carry a stale year the model appended (its cutoff
    anchoring) — that pins the search to old data and returns nothing usable. A year the
    USER asked about, or the current year, is preserved."""
    this_year = datetime.now(UTC).year
    # model bolted "2024" onto a "current" question the user never dated
    assert (
        _strip_unrequested_stale_year("current PM of Nepal 2024", "who is the current PM of Nepal?")
        == "current PM of Nepal"
    )
    # user explicitly asked about a past year → keep it
    assert _strip_unrequested_stale_year("who won in 2019", "who won in 2019?") == "who won in 2019"
    # the current year is not stale → keep it
    q = f"latest results {this_year}"
    assert _strip_unrequested_stale_year(q, "latest results") == q
