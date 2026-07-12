"""User-local time awareness (brief U5; design C5).

The server may run in one timezone while the user lives in another (Spain vs.
Nepal). Greetings and time-of-day references ("morning/evening", "tonight",
"tomorrow", "in 2 hours") must resolve to the USER's local clock, never the
server's. This module computes that local time and an explicit day-part label so
the prompt can anchor the reply — and, when the timezone isn't set on the profile,
DERIVES it from the user's city/country rather than defaulting to the server clock.

If the local time genuinely can't be determined, the caller must NOT assume a
time-of-day (no "good morning" guessing) — that's the exact bug this fixes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from core.profile.models import LocaleProfile

# A question about the CURRENT clock time or today's date — answered DETERMINISTICALLY from the
# `## Right now` prompt block (server clock → user/world timezone), never a web search. A live
# search for the time returns a STALE, cached page whose summary the model then mis-dates (the
# reported bug: "As of Saturday, July 11 … time in Nepal is 11:00 PM" when it was Sunday the
# 12th). Deliberately does NOT match a SCHEDULE question ("what time does the market open?",
# "what time is the meeting?") — those are events, not the clock, and may genuinely need a lookup.
_TIME_OF_DAY_QUERY = re.compile(
    r"\b("
    r"what(?:'?s| is)?\s+the\s+time"  # what's the time
    r"|what\s+time\s+is\s+it"  # what time is it (in X)
    r"|what\s+time\s+do\s+you\s+have"
    r"|(?:the\s+)?(?:current|local)\s+time"  # (the) current/local time
    r"|time\s+(?:right\s+)?now"  # time now / time right now
    r"|(?:current\s+)?time\s+(?:in|at|of)\s+[a-z]"  # time in nepal / time at ...
    r"|what(?:'?s| is)?\s+(?:the|today'?s)\s+date"  # what's the date / today's date
    r"|what\s+day\s+is\s+it"  # what day is it (today)
    r"|what(?:'?s| is)?\s+the\s+day\s+today"
    r")\b",
    re.IGNORECASE,
)


def is_time_of_day_query(utterance: str | None) -> bool:
    """True for a 'what time/date is it (in X)?' question, which is answered from the
    deterministic clock in the prompt — never a web search (which mis-dates a cached page)."""
    return bool(_TIME_OF_DAY_QUERY.search(utterance or ""))


# City → IANA timezone for common cases so a profile that only set city/country
# (not the IANA field) still resolves. Extend as needed; unknown → country lookup.
_CITY_TZ: dict[str, str] = {
    "kathmandu": "Asia/Kathmandu",
    "pokhara": "Asia/Kathmandu",
    "delhi": "Asia/Kolkata",
    "new delhi": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata",
    "dhaka": "Asia/Dhaka",
    "karachi": "Asia/Karachi",
    "london": "Europe/London",
    "madrid": "Europe/Madrid",
    "barcelona": "Europe/Madrid",
    "paris": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "new york": "America/New_York",
    "san francisco": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "toronto": "America/Toronto",
    "dubai": "Asia/Dubai",
    "singapore": "Asia/Singapore",
    "tokyo": "Asia/Tokyo",
    "sydney": "Australia/Sydney",
}

# Country → representative IANA timezone (single-timezone countries are exact;
# multi-timezone countries fall back to the most populous zone). Used only when the
# city didn't resolve.
_COUNTRY_TZ: dict[str, str] = {
    "nepal": "Asia/Kathmandu",
    "india": "Asia/Kolkata",
    "bangladesh": "Asia/Dhaka",
    "pakistan": "Asia/Karachi",
    "sri lanka": "Asia/Colombo",
    "united kingdom": "Europe/London",
    "uk": "Europe/London",
    "spain": "Europe/Madrid",
    "france": "Europe/Paris",
    "germany": "Europe/Berlin",
    "italy": "Europe/Rome",
    "united arab emirates": "Asia/Dubai",
    "uae": "Asia/Dubai",
    "singapore": "Asia/Singapore",
    "japan": "Asia/Tokyo",
    "china": "Asia/Shanghai",
    "australia": "Australia/Sydney",
    "united states": "America/New_York",
    "usa": "America/New_York",
    "canada": "America/Toronto",
}


def resolve_timezone(locale: LocaleProfile | None) -> str | None:
    """The user's IANA timezone: the explicit field first, else derived from
    city/country. None when it genuinely can't be determined."""
    if locale is None:
        return None
    if locale.timezone:
        try:
            ZoneInfo(locale.timezone)
            return locale.timezone
        except Exception:
            pass  # bad value on the profile → fall through to derivation
    city = (locale.city or "").strip().lower()
    if city in _CITY_TZ:
        return _CITY_TZ[city]
    country = (locale.country or "").strip().lower()
    if country in _COUNTRY_TZ:
        return _COUNTRY_TZ[country]
    return None


def day_part(local: datetime) -> str:
    """Human time-of-day label for greetings/references, from the user's local hour."""
    h = local.hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 21:
        return "evening"
    if 21 <= h < 24:
        return "night"
    return "the middle of the night"  # 0-5


def local_now(locale: LocaleProfile | None, now: datetime | None = None) -> datetime | None:
    """The user's current local time, or None if the timezone can't be determined."""
    tz = resolve_timezone(locale)
    if tz is None:
        return None
    base = now or datetime.now(UTC)
    return base.astimezone(ZoneInfo(tz))


# The places a person actually asks the time in. Each is resolved from the maps above, so
# adding a city to `_CITY_TZ` adds it to the world clock too.
_WORLD_CLOCK_PLACES: tuple[tuple[str, str], ...] = (
    ("Nepal", "Asia/Kathmandu"),
    ("India", "Asia/Kolkata"),
    ("UK", "Europe/London"),
    ("Spain", "Europe/Madrid"),
    ("France", "Europe/Paris"),
    ("Germany", "Europe/Berlin"),
    ("UAE", "Asia/Dubai"),
    ("Singapore", "Asia/Singapore"),
    ("Japan", "Asia/Tokyo"),
    ("China", "Asia/Shanghai"),
    ("Australia (Sydney)", "Australia/Sydney"),
    ("US East (New York)", "America/New_York"),
    ("US West (Los Angeles)", "America/Los_Angeles"),
)


def _relative(delta_minutes: int) -> str:
    """ "3h45m behind you" — computed, never asked of the model."""
    if delta_minutes == 0:
        return "same time as you"
    direction = "ahead of" if delta_minutes > 0 else "behind"
    minutes = abs(delta_minutes)
    hours, mins = divmod(minutes, 60)
    span = f"{hours}h{mins:02d}m" if mins else f"{hours}h"
    return f"{span} {direction} you"


def world_clock(user_local: datetime, now: datetime | None = None) -> list[str]:
    """The current local time in each common place, and its offset from the USER (D-17).

    Timezone arithmetic is done here, in code, with `zoneinfo`. The model is handed answers,
    not a puzzle. Asked "what time is it in Spain?" at 3:04 PM Thursday, it previously replied
    "It's still just past midnight on Wednesday" (5 of 10 runs) and gave the relative offset as
    "six hours behind", "three hours behind" and "3 hours ahead" for one true value of
    3h45m behind. It was completing the prompt's worked examples, not computing.
    """
    base = now or datetime.now(UTC)
    user_offset = int((user_local.utcoffset() or timedelta()).total_seconds() // 60)
    lines = []
    for name, tz in _WORLD_CLOCK_PLACES:
        there = base.astimezone(ZoneInfo(tz))
        offset = int((there.utcoffset() or timedelta()).total_seconds() // 60)
        lines.append(
            f"- {name}: {there.strftime('%H:%M')} on {there.strftime('%A')} "
            f"({_relative(offset - user_offset)})"
        )
    return lines
