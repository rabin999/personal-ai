"""Real-call context carrying via the LangGraph orchestrator (A1 graph + A3).

The addendum's headline failure: after giving weather, "what about that
temperature?" used to get "which temperature?". The context-resolution node must
resolve the reference to the SAME weather and answer in context.
"""

import uuid

import pytest

from tests.support.judge import judge_companion_voice

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]


async def test_follow_up_reference_resolves_to_prior_turn(real_turns) -> None:
    session = f"ctx_{uuid.uuid4().hex[:6]}"
    first = await real_turns.say("what's the weather in Kathmandu right now?", session)
    assert first.reply

    # A pure back-reference (recall, not a fresh live-info query): it must resolve
    # "that temperature" to the weather it just gave and repeat/engage it — never
    # ask "which temperature?".
    follow = await real_turns.say("wait, what was that temperature again?", session)
    reply = follow.reply.lower()

    assert "which temperature" not in reply, f"failed to carry context: {follow.reply}"
    assert any(w in reply for w in ("degree", "temperature", "°", "fahrenheit", "celsius")), (
        f"follow-up didn't recall the temperature: {follow.reply}"
    )
    verdict = await judge_companion_voice(
        real_turns.llm, "wait, what was that temperature again?", follow.reply
    )
    assert verdict.ok, f"judged chatbot-like: {verdict.reason} — {follow.reply}"


async def test_context_resolution_node_logged_in_trace(real_turns) -> None:
    session = f"ctxlog_{uuid.uuid4().hex[:6]}"
    await real_turns.say("tell me a fun fact about octopuses.", session)
    await real_turns.say("wait, how many hearts did you say?", session)
    events = await real_turns.traces.traces_for("u_demo_001", session)
    # The graph's context-resolution reasoning is logged (A5 deep trace).
    nodes = [
        e
        for e in events
        if e["stage"] == "reasoning" and e["data"].get("node") == "resolve_context"
    ]
    assert nodes, "context-resolution node not found in the trace"
