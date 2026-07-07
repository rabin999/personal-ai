"""Trace inspection endpoints (brief §1: inspect a turn's full trace after the fact).

Read-only, auth'd, and strictly user-scoped: a user can only ever see their own
persisted traces (§0.5 multi-tenant isolation). Backed by the ``turn_traces``
Mongo collection written during each voice conversation (``core/observability``).
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from api.deps import CurrentUser

router = APIRouter(prefix="/debug")


def _trace_store(request: Request) -> Any:
    pipeline = request.app.state.pipeline
    if pipeline is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="pipeline not wired"
        )
    return pipeline.traces


@router.get("/traces")
async def list_trace_sessions(user: CurrentUser, request: Request) -> dict[str, Any]:
    """This user's recent traced sessions (most recent first)."""
    sessions = await _trace_store(request).recent_sessions(user.user_id)
    return {"user_id": user.user_id, "sessions": sessions}


@router.get("/traces/{session_id}")
async def get_session_trace(
    session_id: str, user: CurrentUser, request: Request
) -> dict[str, Any]:
    """The full A→Z per-turn trace for one of this user's sessions."""
    events = await _trace_store(request).traces_for(user.user_id, session_id)
    return {"user_id": user.user_id, "session_id": session_id, "events": events}
