"""Request-scoped dependencies for the serving edge.

Identity now comes from the signed session cookie established by real Google SSO
(``api/routes/auth.py``), not a bearer token (spec §26, design §18). This
dependency reads the authenticated ``user_id`` from the session and resolves it
to a ``UserRecord`` via the UserContext port; downstream modules take
``user_id`` from here — never hard-code it (spec §0.5). No valid session → 401.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from config.settings import get_settings
from ports.user_context import UserContext, UserRecord

SESSION_USER_KEY = "user_id"


async def get_user_record(request: Request) -> UserRecord:
    """Resolve the request's session to a ``UserRecord`` via the UserContext port."""
    user_context: UserContext | None = request.app.state.user_context
    if user_context is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="UserContext adapter not wired yet",
        )
    # DEV/TEST bypass (empty in prod): resolve to a fixed sample user with no
    # Google session, so the UI can be driven locally over http. Off unless
    # DEV_AUTH_USER is explicitly set.
    dev_user = get_settings().dev_auth_user
    if dev_user:
        return await user_context.record_for(dev_user)
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated — sign in with Google",
        )
    return await user_context.record_for(user_id)


CurrentUser = Annotated[UserRecord, Depends(get_user_record)]
