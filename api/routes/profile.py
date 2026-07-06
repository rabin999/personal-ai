"""Profile route (spec §26/§2): the resolved user's static profile for the UI.

Returns the ``UserRecord`` the bearer token resolves to — the static user data
(companion name, audio prefs, enabled traits, communication prefs) the UI shows
in its profile panel. Read-only; identity is the static stub (§26).
"""

from fastapi import APIRouter

from api.deps import CurrentUser
from ports.user_context import UserRecord

router = APIRouter(prefix="/api")


@router.get("/me")
async def me(user: CurrentUser) -> UserRecord:
    return user
