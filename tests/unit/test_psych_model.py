"""Unit tests for the Psychological User-Model (spec §17) — DocStore faked."""

import re

import pytest

from core.psych.user_model import PsychUserModel, describe_for_prompt
from tests.fakes import FakeDocStore

USER = "u_demo_001"


@pytest.fixture
def psych() -> PsychUserModel:
    return PsychUserModel(FakeDocStore())


# Acceptance: one signal moves confidence only slightly; repetition raises it.
async def test_single_signal_barely_moves_confidence_repeats_raise_it(
    psych: PsychUserModel,
) -> None:
    await psych.update_trait(USER, "openness", 0.8)
    once = (await psych.get(USER)).ocean["openness"]
    assert once.confidence < 0.15  # low early (rule 1)
    assert 0.5 < once.value < 0.6  # nudged, not overwritten

    for _ in range(7):
        await psych.update_trait(USER, "openness", 0.8)
    repeated = (await psych.get(USER)).ocean["openness"]
    assert repeated.confidence > 0.35
    assert repeated.value > 0.7


async def test_contradicting_evidence_lowers_confidence_not_ignored(
    psych: PsychUserModel,
) -> None:
    for _ in range(6):
        await psych.update_trait(USER, "extraversion", 0.9)
    before = (await psych.get(USER)).ocean["extraversion"].confidence

    await psych.update_trait(USER, "extraversion", 0.1)  # sharp contradiction

    after = (await psych.get(USER)).ocean["extraversion"]
    assert after.confidence < before
    assert after.value < 0.9  # estimate moved toward the new evidence


async def test_unknown_trait_rejected(psych: PsychUserModel) -> None:
    with pytest.raises(ValueError):
        await psych.update_trait(USER, "charisma", 0.9)


# Acceptance: mood baseline updates; below-baseline session detectable.
async def test_mood_baseline_rolls_and_detects_low_sessions(
    psych: PsychUserModel,
) -> None:
    for _ in range(5):
        await psych.update_mood(USER, valence=0.4, arousal=0.1)

    baseline = (await psych.get(USER)).mood_baseline
    assert baseline.samples == 5
    assert baseline.valence == pytest.approx(0.4)

    assert await psych.is_below_baseline(USER, valence=0.1) is True
    assert await psych.is_below_baseline(USER, valence=0.38) is False


async def test_no_baseline_means_no_deviation_flag(psych: PsychUserModel) -> None:
    await psych.update_mood(USER, valence=0.5, arousal=0.0)
    assert await psych.is_below_baseline(USER, valence=-0.9) is False  # too few samples


async def test_stage_defaults_and_updates(psych: PsychUserModel) -> None:
    assert await psych.stage(USER, "social_withdrawal") == "precontemplation"
    await psych.set_stage(USER, "social_withdrawal", "contemplation")
    assert await psych.stage(USER, "social_withdrawal") == "contemplation"


async def test_users_never_share_models(psych: PsychUserModel) -> None:
    for _ in range(6):
        await psych.update_trait(USER, "openness", 0.9)
    other = await psych.get("u_demo_002")
    assert other.ocean["openness"].confidence == 0.0


# Acceptance: no output path emits a diagnosis.
async def test_prompt_rendering_never_uses_clinical_language(
    psych: PsychUserModel,
) -> None:
    for _ in range(10):
        await psych.update_trait(USER, "neuroticism", 0.9)
        await psych.update_mood(USER, valence=-0.5, arousal=0.6)
    await psych.set_stage(USER, "social_withdrawal", "contemplation")

    text = describe_for_prompt(await psych.get(USER))

    assert text  # confident signals do render
    banned = r"diagnos|disorder|depress|anxiet|clinical|patholog|illness|symptom"
    assert not re.search(banned, text, flags=re.IGNORECASE)
    assert "tentative" in text or "probabilistic" in text  # hedged framing


async def test_low_confidence_traits_stay_out_of_prompts(psych: PsychUserModel) -> None:
    await psych.update_trait(USER, "openness", 0.9)  # one signal only
    text = describe_for_prompt(await psych.get(USER))
    assert "openness" not in text
