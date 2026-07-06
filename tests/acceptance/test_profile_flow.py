"""Thin e2e for §2: the profile path a real app boot + first request will run.

Startup (db + seeds) → first_run_sync → clamped user update → effective trait
resolution, all through the assembled Database → MongoDocStore → core stack.
"""

import uuid
from pathlib import Path

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from config.settings import Settings
from core.profile import ProfileService, TraitRegistry
from tests.integration.conftest import wait_until_healthy

pytestmark = [pytest.mark.acceptance, pytest.mark.integration]

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"


async def test_profile_lifecycle_end_to_end() -> None:
    database = Database(Settings(_env_file=None))
    user_id = f"it_{uuid.uuid4().hex[:12]}"
    try:
        await wait_until_healthy(database)
        await database.startup()

        store = MongoDocStore(database)
        profiles = ProfileService(store)
        registry = TraitRegistry(store, profiles)
        await registry.seed_defaults(DEFAULTS_DIR)

        profile = await profiles.first_run_sync(user_id)
        assert profile.onboarded is False

        updated = await profiles.update(
            user_id,
            {"companion_name": "Bro", "audio_prefs": {"vad_threshold": 2.0}},
        )
        assert updated.companion_name == "Bro"
        assert updated.audio_prefs.vad_threshold == updated.audio_prefs.vad_max

        trait_ids = {t.id for t in await registry.enabled_traits(user_id)}
        assert {"curiosity_policy", "humor"} <= trait_ids
    finally:
        await database.mongo("user_profile").delete_many({"_id": user_id})
        await database.aclose()
