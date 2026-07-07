"""Port: User Context — authenticated user_id → UserRecord (spec §26, design §18).

Identity is resolved by real Google SSO (``api/routes/auth.py``) into a signed
session cookie carrying our internal ``user_id``; the ``SessionUserContext``
adapter turns that ``user_id`` into a ``UserRecord``. This is the seam §18
anticipated — swapping the identity source left ``core/`` untouched.
"""

from typing import Any, Protocol

from pydantic import BaseModel


class Unauthorized(Exception):
    """Raised when the session carries no / an unknown user (spec §26 rule 2)."""


class UserRecord(BaseModel):
    """Resolved identity + profile, same shape the Profile module (§2) uses.

    Field value types tighten to §2's profile schema when that module is built.
    """

    user_id: str
    companion_name: str | None = None
    audio_prefs: dict[str, Any] = {}
    traits_enabled: dict[str, Any] = {}
    comm_prefs: dict[str, Any] = {}


class UserContext(Protocol):
    """Resolves an authenticated ``user_id`` (from the session) to a ``UserRecord``."""

    async def record_for(self, user_id: str) -> UserRecord:
        """Return the ``UserRecord`` for an authenticated ``user_id``."""
        ...
