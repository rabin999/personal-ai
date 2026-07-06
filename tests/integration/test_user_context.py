"""Integration tests for User Context (spec §26) against real MongoDB."""

from pathlib import Path

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from adapters.user_context.static import StaticUserContext
from core.profile import ProfileService
from ports.user_context import Unauthorized

pytestmark = pytest.mark.integration

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"


@pytest.fixture
async def user_context(db: Database) -> StaticUserContext:
    profiles = ProfileService(MongoDocStore(db))
    return StaticUserContext.from_defaults(DEFAULTS_DIR, profiles)


async def test_resolve_creates_and_reuses_real_profile(
    db: Database, user_context: StaticUserContext
) -> None:
    first = await user_context.resolve("static_token_abc")
    stored = await db.mongo("user_profile").find_one({"_id": first.user_id})
    assert stored is not None and stored["onboarded"] is False

    again = await user_context.resolve("static_token_abc")
    assert again.user_id == first.user_id


async def test_unknown_token_leaves_no_trace(db: Database, user_context: StaticUserContext) -> None:
    before = await db.mongo("user_profile").count_documents({})
    with pytest.raises(Unauthorized):
        await user_context.resolve("stolen-token")
    assert await db.mongo("user_profile").count_documents({}) == before


async def test_two_users_resolve_to_isolated_profiles(
    db: Database, user_context: StaticUserContext
) -> None:
    record_a = await user_context.resolve("static_token_abc")
    record_b = await user_context.resolve("static_token_xyz")
    assert record_a.user_id != record_b.user_id

    profiles = ProfileService(MongoDocStore(db))
    await profiles.update(record_a.user_id, {"companion_name": "OnlyForA"})

    fresh_b = await user_context.resolve("static_token_xyz")
    assert fresh_b.companion_name is None  # A's write never leaks into B
