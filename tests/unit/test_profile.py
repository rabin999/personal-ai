"""Unit tests for Config & User Profile (spec §2) — DocStore port faked in memory."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.profile import ProfileNotFound, ProfileService, TraitRegistry
from core.profile.service import TRAIT_DEFS_COLLECTION

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"


class FakeDocStore:
    """In-memory DocStore implementing the port used by core/profile."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict[str, Any]]] = {}

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        doc = self.collections.get(collection, {}).get(doc_id)
        return dict(doc) if doc is not None else None

    async def put(self, collection: str, doc_id: str, doc: Mapping[str, Any]) -> None:
        stored = {k: v for k, v in doc.items() if k != "_id"} | {"_id": doc_id}
        self.collections.setdefault(collection, {})[doc_id] = stored

    async def find(
        self,
        collection: str,
        query: Mapping[str, Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        docs = list(self.collections.get(collection, {}).values())
        if query:
            docs = [d for d in docs if all(d.get(k) == v for k, v in query.items())]
        return [dict(d) for d in docs[:limit]]


@pytest.fixture
def docs() -> FakeDocStore:
    return FakeDocStore()


@pytest.fixture
def profiles(docs: FakeDocStore) -> ProfileService:
    return ProfileService(docs)


@pytest.fixture
def registry(docs: FakeDocStore, profiles: ProfileService) -> TraitRegistry:
    return TraitRegistry(docs, profiles)


# ── first-run sync (rule 1 / acceptance 1) ───────────────────────────────


async def test_first_run_creates_profile_with_onboarded_false(
    profiles: ProfileService,
) -> None:
    profile = await profiles.first_run_sync("u_demo_001")
    assert profile.user_id == "u_demo_001"
    assert profile.onboarded is False
    assert profile.audio_prefs.vad_threshold == 0.6
    assert profile.created_at  # ISO timestamp set


async def test_second_run_returns_existing_profile_unchanged(
    profiles: ProfileService,
) -> None:
    first = await profiles.first_run_sync("u_demo_001")
    await profiles.update("u_demo_001", {"companion_name": "Bro", "onboarded": True})

    again = await profiles.first_run_sync("u_demo_001")

    assert again.companion_name == "Bro"
    assert again.onboarded is True
    assert again.created_at == first.created_at


async def test_get_without_profile_raises(profiles: ProfileService) -> None:
    with pytest.raises(ProfileNotFound):
        await profiles.get("u_never_synced")


# ── update + VAD clamp (rule 2 / acceptance 2) ───────────────────────────


async def test_update_clamps_vad_threshold_above_max(profiles: ProfileService) -> None:
    await profiles.first_run_sync("u_demo_001")
    updated = await profiles.update("u_demo_001", {"audio_prefs": {"vad_threshold": 0.95}})
    assert updated.audio_prefs.vad_threshold == updated.audio_prefs.vad_max == 0.8


async def test_update_clamps_vad_threshold_below_min(profiles: ProfileService) -> None:
    await profiles.first_run_sync("u_demo_001")
    updated = await profiles.update("u_demo_001", {"audio_prefs": {"vad_threshold": 0.1}})
    assert updated.audio_prefs.vad_threshold == updated.audio_prefs.vad_min == 0.4


async def test_update_patches_section_without_replacing_it(
    profiles: ProfileService,
) -> None:
    await profiles.first_run_sync("u_demo_001")
    updated = await profiles.update("u_demo_001", {"audio_prefs": {"aec": False}})
    assert updated.audio_prefs.aec is False
    assert updated.audio_prefs.noise_suppress is True  # untouched sibling survives


async def test_update_cannot_change_identity_fields(profiles: ProfileService) -> None:
    original = await profiles.first_run_sync("u_demo_001")
    updated = await profiles.update(
        "u_demo_001", {"user_id": "u_evil", "created_at": "1970-01-01T00:00:00+00:00"}
    )
    assert updated.user_id == "u_demo_001"
    assert updated.created_at == original.created_at


async def test_update_rejects_out_of_range_comm_prefs(profiles: ProfileService) -> None:
    await profiles.first_run_sync("u_demo_001")
    with pytest.raises(ValidationError):
        await profiles.update("u_demo_001", {"comm_prefs": {"directness": 1.5}})


# ── trait registry (rules 3-4 / acceptance 3) ────────────────────────────


async def test_enabled_traits_returns_default_enabled_with_description_and_params(
    docs: FakeDocStore, profiles: ProfileService, registry: TraitRegistry
) -> None:
    await registry.seed_defaults(DEFAULTS_DIR)
    await profiles.first_run_sync("u_demo_001")

    traits = await registry.enabled_traits("u_demo_001")

    by_id = {t.id: t for t in traits}
    assert "curiosity_policy" in by_id and "humor" in by_id
    curiosity = by_id["curiosity_policy"]
    assert curiosity.description
    assert curiosity.params["T_intent"] == 0.55


async def test_per_user_override_disables_a_default_enabled_trait(
    profiles: ProfileService, registry: TraitRegistry
) -> None:
    await registry.seed_defaults(DEFAULTS_DIR)
    await profiles.first_run_sync("u_demo_001")
    await profiles.update("u_demo_001", {"traits_enabled": {"humor": False}})

    traits = await registry.enabled_traits("u_demo_001")

    ids = {t.id for t in traits}
    assert "humor" not in ids
    assert "curiosity_policy" in ids  # missing override falls back to default


async def test_seed_defaults_updates_only_on_version_bump(
    docs: FakeDocStore, registry: TraitRegistry, tmp_path: Path
) -> None:
    await registry.seed_defaults(DEFAULTS_DIR)
    # Simulate a live DB edit at the same version: reseeding must not clobber it.
    live = await docs.get(TRAIT_DEFS_COLLECTION, "humor")
    assert live is not None
    live["description"] = "hand-tuned in DB"
    await docs.put(TRAIT_DEFS_COLLECTION, "humor", live)

    await registry.seed_defaults(DEFAULTS_DIR)
    kept = await docs.get(TRAIT_DEFS_COLLECTION, "humor")
    assert kept is not None and kept["description"] == "hand-tuned in DB"

    # A version bump in the seed file wins (spec §2 rule 4).
    bumped = [{"_id": "humor", "version": 99, "default_enabled": True, "description": "v99"}]
    (tmp_path / "trait_defs.json").write_text(json.dumps(bumped))
    await registry.seed_defaults(tmp_path)
    after = await docs.get(TRAIT_DEFS_COLLECTION, "humor")
    assert after is not None and after["description"] == "v99"


async def test_project_types_empty_by_default(registry: TraitRegistry) -> None:
    await registry.seed_defaults(DEFAULTS_DIR)
    assert await registry.project_types() == []
