"""Response feedback endpoints (brief Part C — feedback loop).

Minimalistic: the user may thumbs up/down a response with an optional note. Every
event is captured and tied to the response's trace (session_id / turn_id) so a
thumbs-down is inspectable next to the pipeline that produced it. Auth'd,
user-scoped; optional for the user.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from api.deps import CurrentUser
from core.feedback.store import Rating

router = APIRouter(prefix="/api")


def _store(request: Request) -> Any:
    pipeline = request.app.state.pipeline
    if pipeline is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="pipeline not wired"
        )
    return pipeline.feedback


class _FeedbackIn(BaseModel):
    session_id: str
    rating: Rating
    turn_id: str | None = None
    trace_id: str | None = None
    note: str = ""


@router.post("/feedback")
async def submit_feedback(body: _FeedbackIn, user: CurrentUser, request: Request) -> dict[str, Any]:
    fb = await _store(request).record(
        user_id=user.user_id,
        session_id=body.session_id,
        rating=body.rating,
        turn_id=body.turn_id,
        trace_id=body.trace_id,
        note=body.note,
    )
    # F13 human-in-the-loop: also attach the rating to the corresponding Langfuse
    # trace as a score (up=1 / down=0) so it's inspectable next to the pipeline and
    # can calibrate the LLM-judge. Best-effort — never fails the feedback write.
    pipeline = request.app.state.pipeline
    scores = getattr(pipeline, "scores", None)
    if scores is not None:
        try:
            turn = int(body.turn_id) if body.turn_id and body.turn_id.isdigit() else 0
            scores.score(
                session_id=body.session_id,
                turn=turn,
                name="user_feedback",
                value=1.0 if body.rating == "up" else 0.0,
                comment=body.note,
            )
        except Exception:
            pass
    return {"id": fb.id, "rating": fb.rating}


@router.get("/feedback")
async def list_feedback(
    user: CurrentUser,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    rating: Rating | None = Query(None),  # noqa: B008 (FastAPI query-param default)
) -> dict[str, Any]:
    items, total = await _store(request).list_for_user(
        user.user_id, offset=offset, limit=limit, rating=rating
    )
    return {"total": total, "offset": offset, "limit": limit, "feedback": items}
