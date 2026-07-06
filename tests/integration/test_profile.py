"""Integration tests for Config & User Profile (spec §2) against real MongoDB."""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from core.profile import ProfileService, TraitRegistry
from core.profile.service import (
    PROFILE_COLLECTION,
    PROJECT_TYPES_COLLECTION,
    TRAIT_DEFS_COLLECTION,
)

pytestmark = pytest.mark.integration

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"


@pytest.fixture
async def store(db: Database) -> AsyncIterator[MongoDocStore]:
    yield MongoDocStore(db)
    for collection in (PROFILE_COLLECTION, TRAIT_DEFS_COLLECTION, PROJECT_TYPES_COLLECTION):
        await db.mongo(collection).delete_many({"_id": {"$regex": "^it_"}})


@pytest.fixture
def profiles(store: MongoDocStore) -> ProfileService:
    return ProfileService(store)


@pytest.fixture
def registry(store: MongoDocStore, profiles: ProfileService) -> TraitRegistry:
    return TraitRegistry(store, profiles)


def _user() -> str:
    return f"it_{uuid.uuid4().hex[:12]}"


async def test_first_run_then_second_run_round_trips_through_mongo(
    profiles: ProfileService,
) -> None:
    user_id = _user()
    created = await profiles.first_run_sync(user_id)
    assert created.onboarded is False

    again = await profiles.first_run_sync(user_id)
    assert again == created  # existing profile returned unchanged


async def test_update_clamps_and_persists(profiles: ProfileService) -> None:
    user_id = _user()
    await profiles.first_run_sync(user_id)
    await profiles.update(user_id, {"audio_prefs": {"vad_threshold": 0.99}})

    stored = await profiles.get(user_id)
    assert stored.audio_prefs.vad_threshold == stored.audio_prefs.vad_max


async def test_two_user_isolation_profile_and_overrides_never_cross(
    profiles: ProfileService, registry: TraitRegistry
) -> None:
    user_a, user_b = _user(), _user()
    await registry.seed_defaults(DEFAULTS_DIR)
    await profiles.first_run_sync(user_a)
    await profiles.first_run_sync(user_b)

    await profiles.update(user_a, {"companion_name": "Bro", "traits_enabled": {"humor": False}})

    profile_b = await profiles.get(user_b)
    assert profile_b.companion_name is None
    assert profile_b.traits_enabled == {}
    assert "humor" in {t.id for t in await registry.enabled_traits(user_b)}
    assert "humor" not in {t.id for t in await registry.enabled_traits(user_a)}
