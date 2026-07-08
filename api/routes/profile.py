"""Profile route (spec §26/§2): the resolved user's static profile for the UI.

Returns the ``UserRecord`` the bearer token resolves to — the static user data
(companion name, audio prefs, enabled traits, communication prefs) the UI shows
in its profile panel. Read-only; identity is the static stub (§26).
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

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


@router.get("/tools")
async def external_tools(user: CurrentUser, request: Request) -> dict[str, Any]:
    """F9: URLs of the external tool UIs the app menu links out to — the self-hosted
    Langfuse dashboard (traces/prompts/evals) and, when running, LangGraph Studio.
    Only URLs that are actually configured are returned, so the menu hides the rest."""
    settings = _pipeline(request).settings
    tools: dict[str, str] = {}
    if settings.langfuse_enabled and settings.langfuse_host:
        tools["langfuse"] = settings.langfuse_host.rstrip("/")
    if settings.langgraph_studio_url:
        tools["langgraph"] = settings.langgraph_studio_url.rstrip("/")
    return {"tools": tools}


@router.get("/models")
async def list_models(user: CurrentUser, request: Request) -> dict[str, Any]:
    """The user-selectable fast models + this user's current choice (§4)."""
    pipeline = _pipeline(request)
    profile = await pipeline.profiles.get(user.user_id)
    return {
        "choices": pipeline.llm.fast_model_choices(),
        "selected": profile.model_prefs.fast_model,
        "default": pipeline.llm.route("simple"),
        # F8: the user-selectable mature "thinking"/reasoning model + this user's
        # choice; empty selection → the configured reasoning tier's default model.
        "reasoning_choices": pipeline.llm.reasoning_model_choices(),
        "reasoning_model": profile.model_prefs.reasoning_model,
        "reasoning_default": pipeline.llm.route(pipeline.settings.reasoning_tier),
        # §11: the user-selectable voice engine + this user's persisted choice.
        "voice_engines": ["native", "pipecat"],
        "voice_engine": profile.model_prefs.voice_engine,
    }


@router.patch("/models")
async def set_model(body: dict[str, Any], user: CurrentUser, request: Request) -> dict[str, Any]:
    """Set (or clear, with null) this user's fast/reasoning-model + voice-engine
    choice (§4/§11/F8). A key present with null clears that choice."""
    pipeline = _pipeline(request)
    updates: dict[str, Any] = {}
    if "fast_model" in body:
        fast = body["fast_model"]
        if fast is not None and fast not in pipeline.llm.fast_model_choices():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown model")
        updates["fast_model"] = fast
    if "reasoning_model" in body:
        rm = body["reasoning_model"]
        if rm is not None and rm not in pipeline.llm.reasoning_model_choices():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="unknown reasoning model"
            )
        updates["reasoning_model"] = rm
    if body.get("voice_engine") is not None:
        if body["voice_engine"] not in ("native", "pipecat"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown engine")
        updates["voice_engine"] = body["voice_engine"]
    updated = await pipeline.profiles.update(user.user_id, {"model_prefs": updates})
    return {
        "selected": updated.model_prefs.fast_model,
        "reasoning_model": updated.model_prefs.reasoning_model,
        "voice_engine": updated.model_prefs.voice_engine,
    }


@router.patch("/prefs")
async def set_prefs(body: dict[str, Any], user: CurrentUser, request: Request) -> dict[str, Any]:
    """Set this user's voice playback speed (C7) and locale (C5: timezone/city/country/
    units/currency/language), so the companion frames answers for them. Partial —
    only the provided keys change; the model clamps/validates (speed → [0.8,1.5])."""
    pipeline = _pipeline(request)
    patch: dict[str, Any] = {}
    if "voice_speed" in body:
        try:
            patch["audio_prefs"] = {"voice_speed": float(body["voice_speed"])}
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="bad voice_speed"
            ) from None
    if isinstance(body.get("locale"), dict):
        allowed = {"timezone", "city", "country", "units", "currency", "language"}
        patch["locale"] = {k: v for k, v in body["locale"].items() if k in allowed}
    updated = await pipeline.profiles.update(user.user_id, patch)
    return {
        "voice_speed": updated.audio_prefs.voice_speed,
        "locale": updated.locale.model_dump(),
    }
