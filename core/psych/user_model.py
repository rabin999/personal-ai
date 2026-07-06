"""Psychological User-Model (spec §17): confidence-scored, never diagnostic.

OCEAN trait estimates carry confidence that starts low and rises only with
consistent evidence (rule 1); mood keeps a rolling baseline so deviations
are detectable (rule 2); stage-of-change gates nudge style (rule 4). All of
it is probabilistic signal for tone — never a clinical claim (rule 3).
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from ports.doc_store import DocStore

PSYCH_COLLECTION = "psych_model"

OCEAN_TRAITS = (
    "openness",
    "conscientiousness",
    "extraversion",
    "agreeableness",
    "neuroticism",
)

Stage = Literal["precontemplation", "contemplation", "preparation", "action", "maintenance"]

# Nudge-not-overwrite update rates (rule 1 / §18 rule 2e). Human-tunable.
VALUE_LEARNING_RATE = 0.15
CONFIDENCE_GAIN = 0.08
CONFIDENCE_DECAY = 0.85
CONSISTENCY_THRESHOLD = 0.35

# A session this far below the valence baseline reads as "lower than usual".
MOOD_DEVIATION_THRESHOLD = 0.15

# After this many samples the baseline becomes a slow exponential average so
# it stays adaptive instead of freezing.
_BASELINE_WINDOW = 20
_BASELINE_ALPHA = 0.05


class TraitEstimate(BaseModel):
    value: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class MoodBaseline(BaseModel):
    valence: float = 0.0
    arousal: float = 0.0
    samples: int = 0


class PsychModel(BaseModel):
    user_id: str
    ocean: dict[str, TraitEstimate] = Field(
        default_factory=lambda: {t: TraitEstimate() for t in OCEAN_TRAITS}
    )
    mood_baseline: MoodBaseline = Field(default_factory=MoodBaseline)
    stages: dict[str, Stage] = Field(default_factory=dict)
    updated_at: str = ""


class PsychUserModel:
    def __init__(self, docs: DocStore) -> None:
        self._docs = docs

    async def get(self, user_id: str) -> PsychModel:
        doc = await self._docs.get(PSYCH_COLLECTION, user_id)
        if doc is None:
            return PsychModel(user_id=user_id)
        return PsychModel.model_validate(
            {"user_id": user_id, **{k: v for k, v in doc.items() if k != "_id"}}
        )

    async def update_mood(self, user_id: str, valence: float, arousal: float) -> None:
        """Roll the session's emotional signal into the baseline (rule 2)."""
        model = await self.get(user_id)
        baseline = model.mood_baseline
        baseline.samples += 1
        alpha = 1.0 / baseline.samples if baseline.samples <= _BASELINE_WINDOW else _BASELINE_ALPHA
        baseline.valence += alpha * (valence - baseline.valence)
        baseline.arousal += alpha * (arousal - baseline.arousal)
        await self._save(model)

    async def update_trait(self, user_id: str, trait: str, evidence: float) -> None:
        """Confidence-weighted nudge (rule 1): consistent evidence builds
        confidence slowly; contradicting evidence lowers it rather than being
        ignored (§18 rule 4)."""
        if trait not in OCEAN_TRAITS:
            raise ValueError(f"unknown trait '{trait}'")
        evidence = min(1.0, max(0.0, evidence))
        model = await self.get(user_id)
        estimate = model.ocean[trait]

        consistent = abs(evidence - estimate.value) <= CONSISTENCY_THRESHOLD
        estimate.value += VALUE_LEARNING_RATE * (evidence - estimate.value)
        if consistent:
            estimate.confidence += CONFIDENCE_GAIN * (1.0 - estimate.confidence)
        else:
            estimate.confidence *= CONFIDENCE_DECAY
        await self._save(model)

    async def stage(self, user_id: str, pattern: str) -> Stage:
        model = await self.get(user_id)
        return model.stages.get(pattern, "precontemplation")

    async def set_stage(self, user_id: str, pattern: str, stage: Stage) -> None:
        model = await self.get(user_id)
        model.stages[pattern] = stage
        await self._save(model)

    async def is_below_baseline(self, user_id: str, valence: float) -> bool:
        """True when this session reads lower than the user's usual (rule 2)."""
        model = await self.get(user_id)
        if model.mood_baseline.samples < 3:
            return False  # no meaningful baseline yet
        return valence < model.mood_baseline.valence - MOOD_DEVIATION_THRESHOLD

    async def render_for_prompt(self, user_id: str) -> str:
        """Soft user-model signals for Prompt Assembly (§17 rule 3 → §10).

        Empty until there is confident enough evidence; never a diagnosis.
        """
        return describe_for_prompt(await self.get(user_id))

    async def _save(self, model: PsychModel) -> None:
        model.updated_at = datetime.now(UTC).isoformat()
        doc = model.model_dump()
        doc["_id"] = doc.pop("user_id")
        await self._docs.put(PSYCH_COLLECTION, model.user_id, doc)


def describe_for_prompt(model: PsychModel) -> str:
    """Tentative, non-clinical phrasing for prompt assembly (rule 3).

    Only traits whose confidence clears their weight are mentioned, and only
    as soft tendencies — never labels, never diagnoses.
    """
    lines: list[str] = []
    for trait, estimate in model.ocean.items():
        if estimate.confidence < 0.4:
            continue
        if estimate.value >= 0.6:
            direction = "higher"
        elif estimate.value <= 0.4:
            direction = "lower"
        else:
            direction = None
        if direction:
            lines.append(
                f"- tends toward {direction} {trait} (tentative, confidence "
                f"{estimate.confidence:.1f})"
            )
    if model.mood_baseline.samples >= 3:
        lines.append(
            f"- usual mood baseline: valence {model.mood_baseline.valence:+.2f}, "
            f"arousal {model.mood_baseline.arousal:+.2f}"
        )
    for pattern, stage in model.stages.items():
        lines.append(f"- pattern '{pattern}': stage-of-change {stage} — match nudge style to it")
    if not lines:
        return ""
    return (
        "Soft signals about this user (probabilistic hints, never certainties — "
        "treat as tendencies, not labels):\n" + "\n".join(lines)
    )
