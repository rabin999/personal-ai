"""F4 — real-call coverage of the LIVE VOICE entrypoint (`VoiceSession.converse`).

Before this file, **no test in the repo drove `VoiceSession`**. The real-call suite drove
`orchestrator.generate(...)` — the TEXT path that `api/routes/chat.py` runs — and the latency
harness drove `orchestrator.generate_spoken(...)` with a call shape the voice edge never uses.
So a `TypeError` that made every live voice turn produce silence sat behind a green suite and
a "judge 1.0 PASS" golden set (docs/CODE_FLOW.md §0).

These tests speak real audio at the real session and assert on what actually comes back.
"""

import pytest

from core.reasoning.orchestrator import assert_orchestrator_contract
from tests.support.judge import judge_companion_voice
from tests.support.real_pipeline import RealTurns

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]


async def test_wired_engine_satisfies_the_voice_edge_contract(real_turns: RealTurns) -> None:
    """The regression guard: whatever engine composition wires must accept the exact call
    `VoiceSession._speak_turn` makes. This is the check that would have caught the outage."""
    assert_orchestrator_contract(real_turns.pipeline.orchestrator)


async def test_a_real_voice_turn_produces_audio_and_no_swallowed_errors(
    real_turns: RealTurns,
) -> None:
    cap = await real_turns.say_voice("hi")

    assert cap.exceptions == [], (
        f"the turn path swallowed {len(cap.exceptions)} exception(s): "
        f"{[e['type'] + ': ' + e['exc'] for e in cap.exceptions]}"
    )
    assert cap.audio_chunks > 0 and cap.audio_bytes > 0, (
        "the companion produced NO audio — this is exactly the failure mode that shipped"
    )
    assert cap.transcript.strip(), "STT produced no transcript"
    assert cap.reply_text.strip(), "the companion said nothing"


async def test_the_voice_turn_runs_through_the_wired_orchestrator(real_turns: RealTurns) -> None:
    """The voice path must go through the designed reasoning engine, not around it."""
    cap = await real_turns.say_voice("hi")

    assert cap.graph_nodes == ["perceive", "resolve_context", "respond", "reflect_log"], (
        f"expected the LangGraph turn graph, got {cap.graph_nodes}"
    )
    assert "response" in cap.purposes, f"no response LLM call in {cap.purposes}"


async def test_voice_turn_recalls_a_real_stored_fact(real_turns: RealTurns) -> None:
    """Memory must be read on the VOICE path, not just the text path."""
    cap = await real_turns.say_voice("when do I take my meds?")

    assert cap.exceptions == []
    reply = cap.reply_text.lower()
    assert "8" in reply or "eight" in reply, f"did not recall the real fact: {cap.reply_text!r}"


async def test_voice_reply_sounds_like_the_companion(real_turns: RealTurns) -> None:
    """The judged bar, applied to what the VOICE path actually says."""
    cap = await real_turns.say_voice("hey")

    assert cap.style_flags == [], f"forbidden assistant-speak {cap.style_flags}: {cap.reply_text!r}"
    verdict = await judge_companion_voice(real_turns.pipeline.llm, "hey", cap.reply_text)
    assert not verdict.chatbot_like, f"judged chatbot-like: {verdict.reason} — {cap.reply_text!r}"
    assert verdict.companion_score >= 3, f"score {verdict.companion_score}: {verdict.reason}"
