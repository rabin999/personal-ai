"""Progressive delivery (§8.12 + design "quick empathetic interjection"): a turn that
carries FEELING should meet the emotion FIRST, in the very first thing spoken, before any
facts — delivered as a stream so the person isn't met with dead air. Drives the REAL voice
engine (generate_spoken) and inspects the actual spoken sequence."""

import uuid

import pytest

from tests.support.judge import judge_companion_voice

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]

# Cold assistant-speak that must never be the first thing a hurting person hears.
_COLD_OPENERS = ("i can help", "how can i", "i understand that", "i'm sorry to hear that, but")


async def test_emotional_turn_leads_with_feeling_not_facts(real_turns) -> None:
    session = f"emo_{uuid.uuid4().hex[:6]}"
    msg = "I just found out my dad passed away last night. I don't even know what to do."
    result = await real_turns.say_spoken(msg, session)

    print(f"\n[progressive-delivery] user: {msg}")
    print(f"  spoken sequence ({len(result.spoken)}): {result.spoken}")
    print(f"  full reply: {result.reply}")

    assert result.spoken, "the user heard nothing — no spoken output"
    first = result.spoken[0].strip().lower()
    # The FIRST thing spoken meets the feeling — not a service-desk opener, not a question.
    assert not any(first.startswith(c) for c in _COLD_OPENERS), f"cold opener: {result.spoken[0]}"
    assert not first.endswith("?"), f"led with a question instead of empathy: {result.spoken[0]}"
    verdict = await judge_companion_voice(real_turns.llm, msg, result.reply)
    assert verdict.ok, f"judged not warm/human: {verdict.reason} — {result.reply}"
