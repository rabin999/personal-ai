"""FULL-PATH coverage for time/date questions (the bug that shipped twice because only prompt
fragments were tested, not the wired engine): a "what time/date is it in <place>?" turn — explicit
OR implicit ("is it late in Nepal?") — must be answered from the DETERMINISTIC world clock in the
prompt, with NO web search (a live search returns a stale/rounded page), EXACT to the minute.

Driven through `say_spoken` = the LangGraph orchestrator → generate_spoken, i.e. the same engine
method `voice/session.py` calls in production, against the real model + real stores.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]


def _acceptable_tokens(tz: str) -> set[str]:
    """The HH:MM strings (24h + 12h) the reply may contain. A real turn takes several seconds,
    so the wall clock can tick a minute between the prompt's world_clock and this assertion —
    accept the current minute and the couple before it, since the reply reflects assembly time."""
    now = datetime.now(UTC)
    tokens: set[str] = set()
    for back in range(3):  # this minute and the two before (covers a ~2-min turn)
        t = (now - timedelta(minutes=back)).astimezone(ZoneInfo(tz))
        tokens.add(t.strftime("%H:%M"))
        tokens.add(t.strftime("%-I:%M"))
    return tokens


@pytest.mark.parametrize(
    ("utterance", "tz"),
    [
        ("what time is it in Nepal right now?", "Asia/Kathmandu"),
        ("tell me the current date and time in Nepal", "Asia/Kathmandu"),
        ("what's the time in Kathmandu", "Asia/Kathmandu"),
        ("current time in Japan", "Asia/Tokyo"),
        ("what time is it in New York", "America/New_York"),
        ("is it late in Nepal right now?", "Asia/Kathmandu"),  # implicit — used to search+round
    ],
)
async def test_time_query_uses_the_clock_not_search_and_is_exact(
    real_turns, utterance: str, tz: str
) -> None:
    r = await real_turns.say_spoken(utterance, f"s_time_{abs(hash(utterance))}")
    reply = (" ".join(r.spoken) if r.spoken else r.reply).strip()

    # 1. It must NOT web-search — the server clock is authoritative and exact; a search returns
    #    a stale, rounded page (the reported "Saturday July 11" / "11:00 PM" bugs).
    assert r.searches == [], f"a time question web-searched {r.searches!r}: {reply!r}"

    # 2. It must state the EXACT current minute for that place (never rounded).
    tokens = _acceptable_tokens(tz)
    assert any(tok in reply for tok in tokens), (
        f"reply is not exact to the minute (expected one of {sorted(tokens)}): {reply!r}"
    )


async def test_a_real_current_events_question_STILL_searches(real_turns) -> None:
    """Guard the other side: the time suppression must not have broken normal live-info search.
    A schedule/officeholder question is not a clock reading and still goes to the web."""
    r = await real_turns.say_spoken("who is the current prime minister of Nepal?", "s_pm_ctrl")
    assert r.searches, "a volatile officeholder question must still web-search"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q", "-m", "real_call"])
