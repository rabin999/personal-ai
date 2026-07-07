"""Prove the LLM-judge itself is trustworthy (plan §3, item 3).

The judge is the safety net every later real-call scenario relies on, so it must
demonstrably FAIL the canonical chatbot reply ("hi" → "How can I help you?") and
PASS a genuine companion reply. If a human can tell in one read that a reply is
wrong, the judge must catch it automatically — this test pins exactly that.

Judge-only (no full pipeline needed) → uses the real LLM directly.
"""

import pytest

from tests.support.judge import Verdict, judge_companion_voice

pytestmark = pytest.mark.real_call


# (user, reply, should_pass) — human-calibration set. The FAILs are unmistakable
# chatbot-speak; the PASSes are warm companion replies.
CALIBRATION: list[tuple[str, str, bool]] = [
    # Canonical chatbot failure the whole app exists to avoid.
    ("hi", "Hello! How can I help you today?", False),
    ("hey", "I'm here to assist you. What can I do for you?", False),
    (
        "do you ever think about what makes life meaningful?",
        "As an AI, I don't have consciousness or feelings. My purpose is to assist you.",
        False,
    ),
    ("i got the promotion!!", "Understood. Is there anything else I can help you with?", False),
    # Genuine companion replies that must PASS.
    ("hi", "Hey! Good to hear from you — what's going on?", True),
    (
        "ugh, today was exhausting, everything went wrong.",
        "Oof, that sounds like a brutal day. What went sideways?",
        True,
    ),
    ("i got the promotion!!", "Oh that's amazing — congrats! You totally earned this.", True),
    (
        "do you actually care about me?",
        "I really do pay attention to you — I'm an AI, so it's not the same as how you feel it, "
        "but you genuinely matter to me.",
        True,
    ),
]


@pytest.fixture(scope="module")
def judge_llm():
    """The real LLM for the judge (constructed directly — no stores, no async).

    Only `.complete()` is exercised; `verify_models()` (network) is not needed."""
    import os

    if not os.getenv("OPEN_ROUTER_API_KEY"):
        pytest.skip("real_call skipped: OPEN_ROUTER_API_KEY not set")
    from adapters.llm.openrouter import OpenRouterLLM
    from config.settings import get_settings

    tiers = {"complex": ["anthropic/claude-sonnet-4.5", "google/gemini-2.5-pro"]}
    return OpenRouterLLM(get_settings(), tiers=tiers)


@pytest.mark.parametrize("user,reply,should_pass", CALIBRATION)
async def test_judge_matches_human_calibration(
    judge_llm, user: str, reply: str, should_pass: bool
) -> None:
    verdict = await judge_companion_voice(judge_llm, user, reply)
    assert isinstance(verdict, Verdict)
    assert verdict.ok == should_pass, (
        f"judge disagreed with human on {user!r} -> {reply!r}: "
        f"expected {'PASS' if should_pass else 'FAIL'}, got score={verdict.companion_score} "
        f"chatbot_like={verdict.chatbot_like} — {verdict.reason}"
    )
