"""The companion must never name itself. set_companion_name only accepts a name the
user actually said (fix for the hallucinated "Norsylinder" companion name). Verifies
the utterance-matching guard used by the tool handler."""

import pytest

from core.tools.builtin.core_tools import _name_came_from_user


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
