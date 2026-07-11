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
    """The user-selectable voice engine + this user's persisted choice (§11).

    Fast/reasoning LLM tier selection is backend-only (provider_config.json); the
    engine self-routes by complexity, so those model choices are no longer exposed
    for user selection."""
    pipeline = _pipeline(request)
    profile = await pipeline.profiles.get(user.user_id)
    return {
        # §11: the user-selectable voice engine + this user's persisted choice.
        "voice_engines": ["native", "pipecat"],
        "voice_engine": profile.model_prefs.voice_engine,
    }


@router.patch("/models")
async def set_model(body: dict[str, Any], user: CurrentUser, request: Request) -> dict[str, Any]:
    """Set (or clear, with null) this user's voice-engine choice (§11). Fast/
    reasoning model tiers are backend-only and not settable here."""
    pipeline = _pipeline(request)
    updates: dict[str, Any] = {}
    if body.get("voice_engine") is not None:
        if body["voice_engine"] not in ("native", "pipecat"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown engine")
        updates["voice_engine"] = body["voice_engine"]
    updated = await pipeline.profiles.update(user.user_id, {"model_prefs": updates})
    return {
        "voice_engine": updated.model_prefs.voice_engine,
    }


@router.patch("/prefs")
async def set_prefs(body: dict[str, Any], user: CurrentUser, request: Request) -> dict[str, Any]:
    """Set this user's voice playback speed (C7) and locale (C5: timezone/city/country/
    units/currency/language), so the companion frames answers for them. Partial —
    only the provided keys change; the model clamps/validates (speed → [0.8,1.5])."""
    pipeline = _pipeline(request)
    patch: dict[str, Any] = {}
    audio: dict[str, Any] = {}
    if "voice_speed" in body:
        try:
            audio["voice_speed"] = float(body["voice_speed"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="bad voice_speed"
            ) from None
    # U12 audio-awareness toggle (live per turn — no restart needed). Tone
    # mirroring (mimic_tone) and health check-ins are engine-decided (design §2);
    # their backend fields remain but are no longer user-selectable.
    if "transcribe_others" in body:
        audio["transcribe_others"] = bool(body["transcribe_others"])
    if body.get("ambient_mode") in ("near", "surroundings"):
        audio["ambient_mode"] = body["ambient_mode"]
    if audio:
        patch["audio_prefs"] = audio
    if isinstance(body.get("locale"), dict):
        allowed = {"timezone", "city", "country", "units", "currency", "language"}
        patch["locale"] = {k: v for k, v in body["locale"].items() if k in allowed}
    updated = await pipeline.profiles.update(user.user_id, patch)
    return {
        "voice_speed": updated.audio_prefs.voice_speed,
        "ambient_mode": updated.audio_prefs.ambient_mode,
        "transcribe_others": updated.audio_prefs.transcribe_others,
        "locale": updated.locale.model_dump(),
    }
