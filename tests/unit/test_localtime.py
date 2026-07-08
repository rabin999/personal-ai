"""Unit tests for user-local time awareness (brief U5)."""

from datetime import UTC, datetime

from core.profile.models import LocaleProfile
from core.reasoning.localtime import day_part, local_now, resolve_timezone


def test_explicit_timezone_is_used() -> None:
    assert resolve_timezone(LocaleProfile(timezone="Asia/Kathmandu")) == "Asia/Kathmandu"


def test_timezone_derived_from_city_when_iana_missing() -> None:
    """The demo bug: only city/country set, IANA timezone empty → still resolves."""
    assert resolve_timezone(LocaleProfile(city="Kathmandu", country="Nepal")) == "Asia/Kathmandu"


def test_timezone_derived_from_country() -> None:
    assert resolve_timezone(LocaleProfile(country="Nepal")) == "Asia/Kathmandu"


def test_unknown_locale_returns_none() -> None:
    assert resolve_timezone(LocaleProfile()) is None
    assert resolve_timezone(None) is None
    assert resolve_timezone(LocaleProfile(country="Atlantis")) is None


def test_bad_iana_falls_back_to_derivation() -> None:
    loc = LocaleProfile(timezone="Not/AZone", country="Nepal")
    assert resolve_timezone(loc) == "Asia/Kathmandu"


def test_server_evening_is_user_evening_not_morning() -> None:
    """Server in Spain at 14:30 UTC → Nepal user is at 20:15 = evening, NOT morning
    (this is exactly the reported bug)."""
    server_utc = datetime(2026, 7, 8, 14, 30, tzinfo=UTC)
    local = local_now(LocaleProfile(city="Kathmandu", country="Nepal"), server_utc)
    assert local is not None
    assert local.hour == 20 and local.minute == 15
    assert day_part(local) == "evening"


def test_day_parts() -> None:
    def at(h: int) -> str:
        return day_part(datetime(2026, 7, 8, h, 0, tzinfo=UTC))

    assert at(7) == "morning"
    assert at(14) == "afternoon"
    assert at(19) == "evening"
    assert at(22) == "night"
    assert at(3) == "the middle of the night"
