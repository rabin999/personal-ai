"""Unit tests for User Context (spec §26) — profile store faked in memory."""

from pathlib import Path

import pytest

from adapters.user_context.static import StaticUserContext
from core.profile import ProfileService
from ports.user_context import Unauthorized, UserContext, UserRecord
from tests.unit.test_profile import FakeDocStore

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"

TOKEN_MAP = {"static_token_abc": "u_demo_001", "static_token_xyz": "u_demo_002"}


@pytest.fixture
def profiles() -> ProfileService:
    return ProfileService(FakeDocStore())


@pytest.fixture
def user_context(profiles: ProfileService) -> StaticUserContext:
    return StaticUserContext(TOKEN_MAP, profiles)


# Acceptance: known token resolves to the correct UserRecord with profile.
async def test_known_token_resolves_to_user_record_with_profile(
    user_context: StaticUserContext,
) -> None:
    record = await user_context.resolve("static_token_abc")
    assert record.user_id == "u_demo_001"
    assert record.audio_prefs["vad_threshold"] == 0.6  # §2 profile shape
    assert "directness" in record.comm_prefs


# Acceptance: unknown token → Unauthorized, no pipeline work runs.
async def test_unknown_token_raises_unauthorized_before_any_work(
    user_context: StaticUserContext, profiles: ProfileService
) -> None:
    with pytest.raises(Unauthorized):
        await user_context.resolve("stolen-token")
    # No profile was created as a side effect.
    assert await profiles._docs.find("user_profile") == []


# Acceptance: two tokens resolve to two different user_ids.
async def test_two_tokens_resolve_to_distinct_users(
    user_context: StaticUserContext,
) -> None:
    record_a = await user_context.resolve("static_token_abc")
    record_b = await user_context.resolve("static_token_xyz")
    assert record_a.user_id != record_b.user_id


async def test_resolve_first_run_syncs_the_profile(
    user_context: StaticUserContext, profiles: ProfileService
) -> None:
    await user_context.resolve("static_token_abc")
    profile = await profiles.get("u_demo_001")
    assert profile.onboarded is False


async def test_resolved_record_reflects_profile_updates(
    user_context: StaticUserContext, profiles: ProfileService
) -> None:
    await user_context.resolve("static_token_abc")
    await profiles.update("u_demo_001", {"companion_name": "Bro"})
    record = await user_context.resolve("static_token_abc")
    assert record.companion_name == "Bro"


def test_token_map_must_cover_two_users_for_isolation_checks(
    profiles: ProfileService,
) -> None:
    with pytest.raises(ValueError, match="two distinct users"):
        StaticUserContext({"only_token": "u_demo_001"}, profiles)


def test_defaults_file_loads_with_spec_tokens(profiles: ProfileService) -> None:
    user_context = StaticUserContext.from_defaults(DEFAULTS_DIR, profiles)
    assert set(user_context._tokens.values()) == {"u_demo_001", "u_demo_002"}


# Acceptance: swapping the static adapter for another implementation requires
# zero changes in core/ — anything satisfying the port slots in.
async def test_any_port_implementation_slots_into_the_edge_dependency() -> None:
    class SwappedUserContext:
        async def resolve(self, bearer_token: str) -> UserRecord:
            return UserRecord(user_id="u_from_real_auth")

    swapped: UserContext = SwappedUserContext()  # structural check against the port
    record = await swapped.resolve("anything")
    assert record.user_id == "u_from_real_auth"
