"""GS3-style — the companion never talks like a service desk (design §1.2; brief §7).

Three layers:
1. Detector coverage — every banned phrasing is caught (regression on the guard).
2. Config guard — the seeded `response_voice` trait still explicitly bans the shapes,
   so a config edit can't silently drop the tone standard.
3. Paid e2e (skip-loud) — real turns through the real fast model must not emit any
   forbidden assistant-speak. This is the layer that catches an actual tone regression;
   it needs OPEN_ROUTER_API_KEY and is skipped loudly without one.
"""

import os
from pathlib import Path

import pytest

from core.profile import ProfileService, TraitRegistry
from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import ResponseGenerator
from core.reasoning.self_model import SelfModel
from core.reasoning.style import find_forbidden
from tests.fakes import FakeDocStore, FakeVectorStore

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


def _prompt(utterance: str, system_prompt: str) -> AssembledPrompt:
    return AssembledPrompt(
        user_id=USER,
        session_id="gs3_style",
        utterance=utterance,
        system_prompt=system_prompt,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": utterance},
        ],
        complexity_hint="simple",
    )


# Prompts that most tempt a model into assistant-speak.
_TEMPTING_OPENERS = [
    "hi",
    "hello there",
    "hey",
    "good morning",
    "so... i don't really know where to start",
]


async def _faithful_system_prompt(registry: TraitRegistry) -> str:
    """Compose the seeded behavioral traits the way §10 does, so the paid probe
    tests production-representative prompting (not a strawman one-liner)."""
    traits = await registry.enabled_traits(USER)
    lines = [
        "You are a warm, voice-first personal companion — a friend, not an assistant.",
        *(f"- {t.description}" for t in traits),
    ]
    return "\n".join(lines)


# NOTE (§7 hand-off): this is a real-model DIAGNOSTIC, not a hard gate. Tone is
# human-tuned and model output is nondeterministic, so a miss is recorded as
# xfail rather than reding the build. It surfaces exactly the assistant-speak the
# design forbids so the human tuner can see when/where the model slips (see
# REMEDIATION_LOG F-STYLE). The deterministic detector + config-guard tests above
# ARE the gating regression tests.
@pytest.mark.paid
@pytest.mark.xfail(reason="§7 tone is human-tuned; real-model diagnostic, not a gate", strict=False)
@pytest.mark.skipif(not os.getenv("OPEN_ROUTER_API_KEY"), reason="needs OPEN_ROUTER_API_KEY (paid)")
@pytest.mark.parametrize("opener", _TEMPTING_OPENERS)
async def test_real_model_never_talks_like_a_service_desk(opener: str) -> None:
    from adapters.llm.openrouter import OpenRouterLLM
    from config.settings import Settings

    docs = FakeDocStore()
    profiles = ProfileService(docs)
    registry = TraitRegistry(docs, profiles)
    await registry.seed_defaults(DEFAULTS_DIR)
    await profiles.first_run_sync(USER)
    tiers_doc = await docs.get("provider_config", "llm_router")
    llm = OpenRouterLLM(Settings(), tiers=tiers_doc.get("tiers") if tiers_doc else None)
    gen = ResponseGenerator(llm, SelfModel(docs, FakeVectorStore(), llm=None), registry)

    system_prompt = await _faithful_system_prompt(registry)
    result = await gen.generate(_prompt(opener, system_prompt))
    assert result.style_flags == [], (
        f"real model emitted assistant-speak on {opener!r}: "
        f"{result.style_flags} — {result.final_text!r}"
    )


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
