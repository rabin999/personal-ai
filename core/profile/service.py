"""Config & User Profile services (spec §2).

``ProfileService`` owns the per-user profile lifecycle (first-run sync from
defaults, reads, clamped updates). ``TraitRegistry`` resolves effective traits
(per-user override falling back to the trait default) and app-level project
types, and seeds both collections from ``config/defaults/`` JSON files.
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.profile.models import ProjectType, TraitDef, UserProfile
from ports.doc_store import DocStore

PROFILE_COLLECTION = "user_profile"
TRAIT_DEFS_COLLECTION = "trait_defs"
PROJECT_TYPES_COLLECTION = "project_types"
PROVIDER_CONFIG_COLLECTION = "provider_config"

# Never patchable through profile.update: identity is owned by User Context
# (§26) and creation time is immutable.
_PROTECTED_FIELDS = frozenset({"user_id", "_id", "created_at"})


class ProfileNotFound(KeyError):
    """No profile exists for the user_id (first_run_sync was never called)."""


class ProfileService:
    def __init__(self, docs: DocStore) -> None:
        self._docs = docs

    async def get(self, user_id: str) -> UserProfile:
        doc = await self._docs.get(PROFILE_COLLECTION, user_id)
        if doc is None:
            raise ProfileNotFound(user_id)
        return _profile_from_doc(doc)

    async def first_run_sync(self, user_id: str) -> UserProfile:
        """Create the profile from defaults if absent; existing profiles win (rule 1)."""
        doc = await self._docs.get(PROFILE_COLLECTION, user_id)
        if doc is not None:
            return _profile_from_doc(doc)
        profile = UserProfile(
            user_id=user_id,
            created_at=datetime.now(UTC).isoformat(),
            onboarded=False,
        )
        await self._docs.put(PROFILE_COLLECTION, user_id, _profile_to_doc(profile))
        return profile

    async def update(self, user_id: str, patch: Mapping[str, Any]) -> UserProfile:
        """Apply a partial update; VAD threshold is clamped to [vad_min, vad_max] (rule 2)."""
        current = await self.get(user_id)
        data = current.model_dump()
        for key, value in patch.items():
            if key in _PROTECTED_FIELDS:
                continue
            if isinstance(value, Mapping) and isinstance(data.get(key), dict):
                data[key] = {**data[key], **value}  # section patch, not replace
            else:
                data[key] = value

        audio = data["audio_prefs"]
        audio["vad_threshold"] = min(
            max(audio["vad_threshold"], audio["vad_min"]), audio["vad_max"]
        )
        # C7: clamp playback speed into the valid range BEFORE validation so an
        # out-of-range value is coerced (like vad_threshold), not rejected.
        if "voice_speed" in audio:
            audio["voice_speed"] = min(1.5, max(0.8, float(audio["voice_speed"])))

        profile = UserProfile.model_validate(data)
        await self._docs.put(PROFILE_COLLECTION, user_id, _profile_to_doc(profile))
        return profile


class TraitRegistry:
    def __init__(self, docs: DocStore, profiles: ProfileService) -> None:
        self._docs = docs
        self._profiles = profiles

    async def enabled_traits(self, user_id: str) -> list[TraitDef]:
        """Traits effective for this user: profile override ?? default_enabled (rule 3)."""
        profile = await self._profiles.get(user_id)
        defs = await self._docs.find(TRAIT_DEFS_COLLECTION)
        traits = [_trait_from_doc(doc) for doc in defs]
        return [
            trait for trait in traits if profile.traits_enabled.get(trait.id, trait.default_enabled)
        ]

    async def project_types(self) -> list[ProjectType]:
        docs = await self._docs.find(PROJECT_TYPES_COLLECTION)
        return [ProjectType.model_validate({"id": d["_id"], **_without_id(d)}) for d in docs]

    async def seed_defaults(self, defaults_dir: Path) -> None:
        """Load app-level seeds (trait_defs.json, project_types.json) into Mongo.

        A seed overwrites an existing document only when its ``version`` is
        newer — behavior changes ship as version bumps (rule 4); anything
        edited directly in the DB at the same version is left alone.
        """
        for filename, collection in (
            ("trait_defs.json", TRAIT_DEFS_COLLECTION),
            ("project_types.json", PROJECT_TYPES_COLLECTION),
            ("provider_config.json", PROVIDER_CONFIG_COLLECTION),
        ):
            path = defaults_dir / filename
            if not path.exists():
                continue
            seeds: list[dict[str, Any]] = json.loads(path.read_text())
            for seed in seeds:
                doc_id = seed["_id"]
                existing = await self._docs.get(collection, doc_id)
                if existing is None or seed.get("version", 1) > existing.get("version", 1):
                    await self._docs.put(collection, doc_id, seed)


def _without_id(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k != "_id"}


def _profile_from_doc(doc: Mapping[str, Any]) -> UserProfile:
    return UserProfile.model_validate({"user_id": doc["_id"], **_without_id(doc)})


def _profile_to_doc(profile: UserProfile) -> dict[str, Any]:
    doc = profile.model_dump()
    doc["_id"] = doc.pop("user_id")
    return doc


def _trait_from_doc(doc: Mapping[str, Any]) -> TraitDef:
    return TraitDef.model_validate({"id": doc["_id"], **_without_id(doc)})
