"""Adapter: static bearer-token → UserRecord (implements ports.user_context.UserContext, spec §26).

The ONLY stubbed identity source in the system. Resolves a token against a
static map (config/defaults/static_users.json), first-run-syncs that user's
profile (§2), and returns a UserRecord with the same profile shape production
auth will use. Real authentication later = replace this adapter; nothing in
``core/`` changes.
"""

import json
from collections.abc import Mapping
from pathlib import Path

from core.profile import ProfileService
from ports.user_context import Unauthorized, UserRecord

STATIC_USERS_FILE = "static_users.json"


class StaticUserContext:
    def __init__(self, token_map: Mapping[str, str], profiles: ProfileService) -> None:
        if len(set(token_map.values())) < 2:
            # Spec §26 rule 4: at least two users so multi-tenant isolation
            # stays verifiable by hand.
            raise ValueError("static token map must define at least two distinct users")
        self._tokens = dict(token_map)
        self._profiles = profiles

    @classmethod
    def from_defaults(cls, defaults_dir: Path, profiles: ProfileService) -> "StaticUserContext":
        token_map: dict[str, str] = json.loads((defaults_dir / STATIC_USERS_FILE).read_text())
        return cls(token_map, profiles)

    async def resolve(self, bearer_token: str) -> UserRecord:
        """Token → UserRecord; unknown tokens fail before any pipeline work (rule 2)."""
        user_id = self._tokens.get(bearer_token)
        if user_id is None:
            raise Unauthorized("unknown bearer token")
        profile = await self._profiles.first_run_sync(user_id)
        return UserRecord(
            user_id=user_id,
            companion_name=profile.companion_name,
            audio_prefs=profile.audio_prefs.model_dump(),
            traits_enabled=dict(profile.traits_enabled),
            comm_prefs=profile.comm_prefs.model_dump(),
        )
