"""Per-user conversation history (spec §6; supports the /conversations view).

Read-only, auth'd, strictly user-scoped (§0.5): a user sees only their own
conversations. Server-side pagination AND server-side datetime-range filtering
(the store filters on an epoch field; no client-side date math). ``from``/``to``
accept ISO-8601 timestamps (e.g. ``2026-07-01T00:00:00Z``).
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from api.deps import CurrentUser

router = APIRouter(prefix="/api")


def _conversations(request: Request) -> Any:
    pipeline = request.app.state.pipeline
    if pipeline is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="pipeline not wired"
        )
    return pipeline.conversations


def _parse_ts(value: str | None, field: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be an ISO-8601 datetime",
        ) from exc


@router.get("/conversations")
async def list_conversations(
    user: CurrentUser,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
) -> dict[str, Any]:
    """This user's conversations, newest first, paginated + datetime-range filtered."""
    store = _conversations(request)
    page, total = await store.list_conversations(
        user.user_id,
        offset=offset,
        limit=limit,
        start_ts=_parse_ts(from_, "from"),
        end_ts=_parse_ts(to, "to"),
    )
    return {"total": total, "offset": offset, "limit": limit, "conversations": page}


@router.get("/conversations/{session_id}")
async def get_conversation(
    session_id: str,
    user: CurrentUser,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """The verbatim turns of one of this user's conversations."""
    store = _conversations(request)
    turns = await store.turns(user.user_id, session_id, offset=offset, limit=limit)
    return {"session_id": session_id, "turns": turns}
