"""Data schemas for Config & User Profile (spec §2)."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AudioPrefs(BaseModel):
    """Per-user audio pipeline settings; VAD threshold is clamped to [vad_min, vad_max]."""

    vad_threshold: float = 0.6
    vad_min: float = 0.4
    vad_max: float = 0.8
    # §21/brief §2.3: tightened from 700ms — start generating ~100ms sooner after
    # a complete thought without cutting mid-sentence (the long pause still guards
    # trailing "and…"/filler). Per-user tunable and learnable later (§18).
    endpoint_short_pause_ms: int = 600
    endpoint_long_pause_ms: int = 2500
    aec: bool = True
    noise_suppress: bool = True
    agc: bool = True


class CommPrefs(BaseModel):
    directness: float = Field(default=0.5, ge=0.0, le=1.0)
    emotional_scaffolding: float = Field(default=0.5, ge=0.0, le=1.0)


class ModelPrefs(BaseModel):
    """Per-user LLM model choice (§4). ``fast_model`` is a user-selected fast/flash
    model tried first on non-complex turns; None → default tier routing. The
    router validates it against the configured catalog, so a stale id is ignored."""

    fast_model: str | None = None


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
