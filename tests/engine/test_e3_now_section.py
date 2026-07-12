"""D-17 — the prompt's worked examples were being spoken as the answer.

`localtime_spain` passed the gate 10/10 while replying *"It's still just past midnight on
Wednesday in Spain"* at **3:04 PM Thursday**. Five of ten replies named the wrong day.

The prompt was not missing anything. It carried the UTC clock, the weekday, the user's
timezone — and this:

    STATE the actual clock time in a natural human way
    (e.g. 'just past midnight', 'about half four in the afternoon')
    ...
    When they ask the time elsewhere, also say it relative to them ('~3 hours ahead of you').

`'just past midnight'` appears verbatim in **4 of 10 replies** as the time. `"3 hours ahead of
you"` appears in one, pointing the wrong way: Spain is 3h45m *behind* Kathmandu. The model was
completing the illustration rather than the task.

Two fixes, and the second is the one that matters:

1. The worked examples are gone. An example of *how to phrase an answer* sitting next to the
   data is indistinguishable from the answer.
2. The timezone arithmetic is done in `world_clock()` with `zoneinfo`, and the model is handed
   the converted times and the exact offset from the user. It reads them off. Asking a language
   model to subtract 5:45 from 2:00 and report the direction produced "six hours behind",
   "three hours behind" and "3 hours ahead" for one true value.

The gate check was also too weak: it forbade the string `utc+` and nothing else, so a reply
naming the wrong day passed. `must_state_spanish_time()` now validates against `zoneinfo`.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from core.profile.models import LocaleProfile
from core.reasoning.localtime import _relative, local_now, world_clock
from core.reasoning.prompt_assembly import _now_section

KATHMANDU = LocaleProfile(timezone="Asia/Kathmandu", city="Kathmandu", country="Nepal")

# The exact strings the prompt used to offer, and 4 of 10 replies then spoke.
WORKED_EXAMPLES = [
    "just past midnight",
    "half four in the afternoon",
    "hours ahead of you')",
    "Tokyo = UTC+9",
    "Kathmandu = UTC+5:45",
]


@pytest.mark.parametrize("example", WORKED_EXAMPLES, ids=lambda e: e[:24])
def test_the_prompt_offers_no_worked_example_of_an_answer(example: str) -> None:
    """An example of how to phrase the answer, sitting beside the data, is the answer."""
    section, _signal = _now_section(KATHMANDU)
    assert example not in section, (
        f"the `## Right now` block still hands the model {example!r} to complete. "
        "See docs/DEFECTS_FOUND.md D-17."
    )


def test_the_prompt_still_carries_the_facts_it_needs() -> None:
    """Removing the examples must not remove the data. The UTC clock, the weekday and the
    user's own local time are what the model actually needs."""
    now = datetime.now(UTC)
    section, signal = _now_section(KATHMANDU)

    assert now.strftime("%Y-%m-%d %H:%M") in section
    assert now.strftime("%A") in section
    assert "Asia/Kathmandu" in section
    assert signal and signal.startswith("localtime=")


# ── the arithmetic is done in code ───────────────────────────────────────────


def test_the_world_clock_states_the_real_time_in_spain() -> None:
    """The failing scenario, checked against `zoneinfo` rather than against the absence of
    the string "utc+"."""
    now = datetime.now(UTC)
    lines = world_clock(local_now(KATHMANDU, now), now)
    spain = next(line for line in lines if line.startswith("- Spain:"))

    expected = now.astimezone(ZoneInfo("Europe/Madrid"))
    assert expected.strftime("%H:%M") in spain
    assert expected.strftime("%A") in spain, "the world clock names the wrong day"


def test_spain_is_reported_as_behind_kathmandu_not_ahead() -> None:
    """Kathmandu is UTC+5:45 and Spain is UTC+2, so Spain is 3h45m BEHIND. The engine said
    "six hours behind", "three hours behind" and "3 hours ahead" — for one true value, across
    runs of the same question."""
    now = datetime.now(UTC)
    spain = next(
        line for line in world_clock(local_now(KATHMANDU, now), now) if line.startswith("- Spain:")
    )
    assert "behind you" in spain
    assert "ahead of you" not in spain


def test_the_offset_is_exact_to_the_minute() -> None:
    """India is 15 minutes behind Nepal. A model asked to do this arithmetic rounds it away;
    `zoneinfo` does not."""
    now = datetime.now(UTC)
    lines = world_clock(local_now(KATHMANDU, now), now)
    india = next(line for line in lines if line.startswith("- India:"))
    assert "0h15m behind you" in india


@pytest.mark.parametrize(
    "delta,expected",
    [
        (0, "same time as you"),
        (-225, "3h45m behind you"),
        (135, "2h15m ahead of you"),
        (-120, "2h behind you"),
        (60, "1h ahead of you"),
    ],
)
def test_relative_offsets_read_the_way_a_person_would_say_them(delta: int, expected: str) -> None:
    assert _relative(delta) == expected


def test_the_user_appears_in_their_own_world_clock_as_the_same_time() -> None:
    """A sanity anchor: whatever else drifts, the user is not ahead of themselves."""
    now = datetime.now(UTC)
    nepal = next(
        line for line in world_clock(local_now(KATHMANDU, now), now) if line.startswith("- Nepal:")
    )
    assert "same time as you" in nepal


def test_a_place_not_on_the_clock_is_admitted_to_rather_than_guessed() -> None:
    """§16. The list is finite; the world is not. Say so, instead of doing the arithmetic that
    produced "just past midnight on Wednesday". The world clock (with this guidance) is injected
    only on a time question."""
    section, _signal = _now_section(KATHMANDU, "what time is it in Iceland?")
    assert "not sure of the exact time there" in section


def test_world_clock_is_injected_only_on_a_time_question() -> None:
    """The full 13-place clock is ~700 chars, so it's lean: present on a time/date question,
    absent otherwise (the anchor for the user's OWN time-of-day stays either way)."""
    on_time, _ = _now_section(KATHMANDU, "what's the date and time in Japan?")
    off_time, _ = _now_section(KATHMANDU, "i had a rough day at work")
    assert "- Japan:" in on_time and "- Nepal:" in on_time
    assert "- Japan:" not in off_time  # not carried on a non-time turn
    assert "FOR THE USER" in off_time  # but the user's own local anchor still is


def test_no_locale_user_still_gets_an_exact_country_time() -> None:
    """The reported bug: a user with NO locale asking the Nepal time got a fabricated "5:45am"
    (the +5:45 offset). The absolute clock needs nothing from their profile."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(UTC)
    section, signal = _now_section(None, "what time is it in nepal right now")
    assert signal is None  # their own time-of-day is unknown → no greeting anchor
    nepal_hhmm = now.astimezone(ZoneInfo("Asia/Kathmandu")).strftime("%H:%M")
    assert f"- Nepal: {nepal_hhmm}" in section  # exact, no offset-as-clock fabrication
