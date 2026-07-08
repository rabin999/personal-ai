"""Data schemas for Config & User Profile (spec §2)."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AudioPrefs(BaseModel):
    """Per-user audio pipeline settings; VAD threshold is clamped to [vad_min, vad_max]."""

    vad_threshold: float = 0.6
    vad_min: float = 0.4
    vad_max: float = 0.8
    # §21: silence after a COMPLETE-sounding thought before we take our turn. Raised
    # to 900ms — 600ms cut people off when they paused mid-thought (a beat to think);
    # 900ms gives natural pauses room while the long pause still guards trailing
    # "and…"/filler for clearly-incomplete thoughts. Per-user tunable (§18).
    endpoint_short_pause_ms: int = 900
    endpoint_long_pause_ms: int = 2500
    aec: bool = True
    noise_suppress: bool = True
    agc: bool = True
    # §24: how far below the turn-start VAD gate barge-in listens while the
    # companion speaks (AEC has removed our own TTS, so a lower bar catches the
    # user's double-talk-attenuated speech). Per-user tunable; floored at vad_min.
    barge_in_sensitivity: float = 0.2
    # C7: TTS playback rate multiplier (1.0 = normal, the default — >1.0 shifts pitch
    # up and can sound chipmunk-y). Clamped to [0.8, 1.5] on write; applied to both
    # voice engines behind the voice port and recorded in the trace.
    voice_speed: float = Field(default=1.0, ge=0.8, le=1.5)


class LocaleProfile(BaseModel):
    """Who/where the user is, so answers can be framed for THEM (C5): times in their
    local clock + relative to their timezone, temperatures/distances in their unit
    system, money in their currency. Empty fields mean 'unknown' — the companion
    then asks once (with consent) or falls back to neutral phrasing, never guesses a
    wrong locale. All optional; captured from the profile UI or inferred with consent."""

    timezone: str = ""  # IANA, e.g. "Asia/Kathmandu"
    city: str = ""
    country: str = ""
    units: Literal["metric", "imperial", ""] = ""
    currency: str = ""  # ISO 4217, e.g. "NPR", "USD"
    language: str = ""  # e.g. "en", "ne"


class CommPrefs(BaseModel):
    directness: float = Field(default=0.5, ge=0.0, le=1.0)
    emotional_scaffolding: float = Field(default=0.5, ge=0.0, le=1.0)


class ModelPrefs(BaseModel):
    """Per-user LLM + voice-engine choice (§4/§11). ``fast_model`` is a user-selected
    fast/flash model tried first on non-complex turns; None → default tier routing.
    ``voice_engine`` selects the voice runtime (native asyncio loop vs Pipecat); it
    persists so the client reconnects to the same engine, and it's recorded in the
    trace. Both are validated so a stale value is ignored."""

    fast_model: str | None = None
    # F8: the user-selected mature "thinking" model for the main reasoning turn
    # (A2). None → the configured reasoning tier's default. Validated on write.
    reasoning_model: str | None = None
    voice_engine: Literal["native", "pipecat"] = "native"


class UserProfile(BaseModel):
    """Live source of truth for one user's settings (Mongo ``user_profile``).

    ``traits_enabled`` holds per-user *overrides* only; a trait missing here
    falls back to its ``TraitDef.default_enabled`` (spec §2 rule 3).
    """

    user_id: str
    companion_name: str | None = None
    audio_prefs: AudioPrefs = Field(default_factory=AudioPrefs)
    traits_enabled: dict[str, bool] = Field(default_factory=dict)
    comm_prefs: CommPrefs = Field(default_factory=CommPrefs)
    model_prefs: ModelPrefs = Field(default_factory=ModelPrefs)
    locale: LocaleProfile = Field(default_factory=LocaleProfile)  # C5: know the user
    created_at: str
    onboarded: bool = False


class TraitDef(BaseModel):
    """One configurable behavior trait (Mongo ``trait_defs``).

    ``description`` is the natural-language behavior spec injected into the
    system prompt; changing behavior = edit description/params + bump version
    (spec §2 rule 4). No code change.
    """

    id: str
    version: int = 1
    default_enabled: bool = True
    description: str
    params: dict[str, Any] = Field(default_factory=dict)


class ProjectType(BaseModel):
    """Project *type* definition (Mongo ``project_types``); schema is owned by §16."""

    model_config = ConfigDict(extra="allow")

    id: str
