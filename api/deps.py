"""Request-scoped dependencies for the serving edge.

The token → user_id wiring point (spec §26 rule 1): every request carries a
bearer token; this dependency resolves it through the UserContext port and
the resulting ``user_id`` flows into the pipeline. Downstream modules must
take ``user_id`` from here — never hard-code it (CLAUDE.md §2.2).
"""

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from ports.user_context import Unauthorized, UserContext, UserRecord


async def get_user_record(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> UserRecord:
    """Resolve the request's bearer token to a ``UserRecord`` via the UserContext port."""
    user_context: UserContext | None = request.app.state.user_context
    if user_context is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="UserContext adapter not wired yet (spec §26 not built)",
        )
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await user_context.resolve(token)
    except Unauthorized as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


CurrentUser = Annotated[UserRecord, Depends(get_user_record)]
