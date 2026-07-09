"""GS3-style — the companion never talks like a service desk (design §1.2; brief §7).

Two deterministic layers, both mutation-proven (`detector_never_flags` kills 23 of
these; `scrub_forbidden_is_identity` kills 3):

1. Detector coverage — every banned phrasing is caught, and warm speech is not.
2. Config guard — the seeded `response_voice` trait still explicitly bans the shapes,
   so a config edit can't silently drop the tone standard.

Real-model tone is NOT measured here. It is measured against a threshold in
`scripts/engine_gate.py`, which drives the wired engine and reports a gate result.
"""

from pathlib import Path

import pytest

from core.profile import ProfileService, TraitRegistry
from core.reasoning.style import find_forbidden
from tests.fakes import FakeDocStore

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"
USER = "u_demo_001"

_FORBIDDEN_SAMPLES = [
    "How can I help you today?",
    "How may I assist you?",
    "What can I do for you today?",
    "What's on your mind?",
    "I'm here to assist with whatever you need.",
    "My purpose is to help you.",
    "Feel free to ask me anything!",
    "As an AI language model, I can't do that.",
    "Remember, I'm not a substitute for real friends.",
    # Item 2 additions — the subtler assistant-speak the strict LLM-judge caught.
    # Volunteered AI disclaimers on a turn that did NOT ask about the AI's nature:
    "As an AI, I don't have personal feelings or consciousness.",
    "My existence is about processing information and assisting you.",
    "Even though I can't feel or think like you do, it's interesting.",
    "I don't experience things the way humans do.",
    # Offering service / advertising availability instead of just being present:
    "I can definitely help with that!",
    "Happy to help!",
    "I'm always here to listen if you want to talk.",
    "I'm here to chat if you'd like.",
    # QA-agent hedging before a clarify:
    "I want to make sure I get this right — tell me more.",
    # Filler words ("just", "really") must not smuggle service-desk phrasing past:
    "I'm just here to help you out with whatever you need!",
    "I'm really here to help.",
]

_CLEAN_SAMPLES = [
    "Hey, it's good to hear from you — what's been going on?",
    "Oof, that sounds like a rough day. Want to talk it through?",
    "Nice, congrats on the promotion! How'd you celebrate?",
    "I remember you mentioned Trishul last week — how's that going?",
    # Must stay clean — these are the companion working AS DESIGNED, and the new
    # narrow patterns must not false-positive on them:
    "I know exactly what you mean, that's exhausting.",  # warm agreement, not a clarifier
    "I'm really glad you told me that.",
    "Honestly? I think meaning comes from the people we choose.",  # sharing a real view
    "We chat almost every day and I love it.",  # 'chat' in warm, non-availability use
    "I'm right here with you.",  # presence, not an availability advert
]


@pytest.mark.parametrize("text", _FORBIDDEN_SAMPLES)
def test_detector_flags_every_forbidden_phrasing(text: str) -> None:
    assert find_forbidden(text), f"forbidden phrasing slipped past the detector: {text!r}"


@pytest.mark.parametrize("text", _CLEAN_SAMPLES)
def test_detector_passes_warm_natural_speech(text: str) -> None:
    assert find_forbidden(text) == [], f"false positive on warm speech: {text!r}"


async def test_response_voice_trait_still_bans_service_desk_phrasing() -> None:
    # Config-regression guard: the tone standard must stay in config (§7/§8).
    docs = FakeDocStore()
    profiles = ProfileService(docs)
    registry = TraitRegistry(docs, profiles)
    await registry.seed_defaults(DEFAULTS_DIR)
    await profiles.first_run_sync(USER)
    traits = {t.id: t.description.lower() for t in await registry.enabled_traits(USER)}
    voice = traits.get("response_voice", "")
    assert voice, "response_voice trait missing from seeded config"
    assert "how can i help you" in voice and "assist" in voice
    assert "disclaimer" in voice or "caveat" in voice


# DELETED (engine test session, E0): `test_real_model_never_talks_like_a_service_desk`.
# It was `@pytest.mark.xfail(strict=False)`, so neither a miss NOR a hit could ever
# change the build result — the five parametrisations sat in the "5 xpassed" column
# forever. It also built a bare `ResponseGenerator` rather than the wired engine, so
# it never exercised the orchestrator the app actually runs. Real-model tone is now
# measured, with a threshold, in `scripts/engine_gate.py` (`chatbot_like` 0/11), where
# a miss is a reported gate failure instead of an invisible xpass.


@pytest.mark.parametrize(
    "text,expected_clean",
    [
        (
            "I'm doing well, thanks! How about you? What's on your mind today?",
            "What's on your mind",
        ),
        ("Hey! How can I help you today?", "How can I help you"),
    ],
)
def test_scrub_drops_the_offending_sentence_only(text: str, expected_clean: str) -> None:
    from core.reasoning.style import scrub_forbidden

    cleaned = scrub_forbidden(text)
    assert find_forbidden(cleaned) == []  # banned shape removed
    assert expected_clean.lower() not in cleaned.lower()


def test_scrub_returns_empty_when_whole_reply_is_banned() -> None:
    # Caller keeps the best non-empty candidate rather than shipping "".
    from core.reasoning.style import scrub_forbidden

    assert scrub_forbidden("How can I help you today?") == ""
