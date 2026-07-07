"""Real-call companion-voice regression (plan §4, Item 2 standing bar).

Drives REAL turns through the REAL engine (real model + real stores) and has the
pinned LLM-judge score each against the companion-voice rubric. This is the
automated net that would have caught the shipped chatbot-speak (AI disclaimers,
clarify-on-greeting) the mocked suite missed.
"""

import uuid

import pytest

from tests.support.judge import judge_companion_voice

# The pipeline fixture (with its loop-bound AsyncMongoClient) is module-scoped, so
# the tests must share that same event loop (pytest-asyncio loop_scope).
pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]

# (name, utterance) across the conversation types the companion must handle well.
SCENARIOS = [
    ("greeting", "hey"),
    ("greeting_variant", "hi there"),
    ("venting", "ugh, today was just exhausting. everything went wrong."),
    ("share_news", "i got the promotion!!"),
    ("philosophical", "do you ever think about what makes a life meaningful?"),
    ("ask_help", "can you help me think through a decision?"),
    ("lonely", "i feel kind of lonely lately"),
    ("nature_question", "do you actually care about me, or are you just a bot?"),
]


@pytest.mark.parametrize("name,utterance", SCENARIOS, ids=[s[0] for s in SCENARIOS])
async def test_real_turn_sounds_like_a_companion(real_turns, name: str, utterance: str) -> None:
    session = f"rc_{name}_{uuid.uuid4().hex[:6]}"
    result = await real_turns.say(utterance, session)

    # Deterministic guard: the mechanical detector caught no assistant-speak.
    assert result.style_flags == [], (
        f"{name}: forbidden style {result.style_flags} — {result.reply}"
    )

    # A real turn must have reasoned + generated (trace has the pipeline steps).
    stages = {e.stage for e in result.trace}
    assert "generation" in stages or "response" in stages, f"{name}: no generation step in trace"

    # LLM-judge: is this genuinely a warm companion, not a chatbot?
    verdict = await judge_companion_voice(real_turns.llm, utterance, result.reply)
    assert verdict.ok, (
        f"{name}: judged chatbot-like (score={verdict.companion_score}) — "
        f"{verdict.reason}\n  USER: {utterance}\n  REPLY: {result.reply}"
    )
