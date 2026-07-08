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

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from core.profile.models import LocaleProfile

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
