"""Adapter: session user_id → UserRecord (implements ports.user_context, §26).

The real-auth replacement for the static token resolver. Identity now comes from
the signed session cookie (our internal ``user_id``); this adapter turns that
``user_id`` into the same ``UserRecord`` shape the rest of the app already reads,
so nothing in ``core/`` changes — exactly the seam §18 anticipated.
"""

from core.profile import ProfileService
from ports.user_context import UserRecord


class SessionUserContext:
    def __init__(self, profiles: ProfileService) -> None:
        self._profiles = profiles

    async def record_for(self, user_id: str) -> UserRecord:
        """Build the resolved identity + profile for an authenticated user_id."""
        profile = await self._profiles.first_run_sync(user_id)
        return UserRecord(
            user_id=user_id,
            companion_name=profile.companion_name,
            audio_prefs=profile.audio_prefs.model_dump(),
            traits_enabled=dict(profile.traits_enabled),
            comm_prefs=profile.comm_prefs.model_dump(),
        )
