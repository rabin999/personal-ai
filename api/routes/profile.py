"""Profile route (spec §26/§2): the resolved user's static profile for the UI.

Returns the ``UserRecord`` the bearer token resolves to — the static user data
(companion name, audio prefs, enabled traits, communication prefs) the UI shows
in its profile panel. Read-only; identity is the static stub (§26).
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from api.deps import CurrentUser
from ports.user_context import UserRecord

router = APIRouter(prefix="/api")


@router.get("/me")
async def me(user: CurrentUser) -> UserRecord:
    return user


def _pipeline(request: Request) -> Any:
    pipeline = request.app.state.pipeline
    if pipeline is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="pipeline not wired"
        )
    return pipeline


@router.get("/models")
async def list_models(user: CurrentUser, request: Request) -> dict[str, Any]:
    """The user-selectable fast models + this user's current choice (§4)."""
    pipeline = _pipeline(request)
    profile = await pipeline.profiles.get(user.user_id)
    return {
        "choices": pipeline.llm.fast_model_choices(),
        "selected": profile.model_prefs.fast_model,
        "default": pipeline.llm.route("simple"),
        # §11: the user-selectable voice engine + this user's persisted choice.
        "voice_engines": ["native", "pipecat"],
        "voice_engine": profile.model_prefs.voice_engine,
    }


class _ModelChoice(BaseModel):
    fast_model: str | None = None
    voice_engine: str | None = None


@router.patch("/models")
async def set_model(body: _ModelChoice, user: CurrentUser, request: Request) -> dict[str, Any]:
    """Set (or clear, with null) this user's fast-model + voice-engine choice (§4/§11)."""
    pipeline = _pipeline(request)
    updates: dict[str, Any] = {}
    if body.fast_model is not None:
        if body.fast_model not in pipeline.llm.fast_model_choices():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown model")
        updates["fast_model"] = body.fast_model
    if body.voice_engine is not None:
        if body.voice_engine not in ("native", "pipecat"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown engine")
        updates["voice_engine"] = body.voice_engine
    updated = await pipeline.profiles.update(user.user_id, {"model_prefs": updates})
    return {
        "selected": updated.model_prefs.fast_model,
        "voice_engine": updated.model_prefs.voice_engine,
    }
