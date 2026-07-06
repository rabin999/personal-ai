"""Port: User Context — bearer token → UserRecord (spec §26, design doc §18).

The ONLY place identity is stubbed. The static adapter
(``adapters/user_context/static.py``) resolves a static token map; real auth
later replaces that adapter with zero changes in ``core/``.
"""

from typing import Any, Protocol

from pydantic import BaseModel


class Unauthorized(Exception):
    """Raised by ``resolve`` when the bearer token is unknown (spec §26 rule 2)."""


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
    """Resolves an incoming bearer token to a ``UserRecord``."""

    async def resolve(self, bearer_token: str) -> UserRecord:
        """Return the ``UserRecord`` for ``bearer_token``; raise ``Unauthorized`` if unknown."""
        ...
