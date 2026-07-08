"""Real-call intent inference for INDIRECT/implicit requests (F5).

The companion must infer what an indirect message really wants, decide whether it
needs current info, meet the emotional weight — and LOG the inferred intent + why
in the trace. Real model + real stores.
"""

import uuid

import pytest

from tests.support.judge import judge_companion_voice

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]


def _resolve_span(events: list[dict]) -> dict:
    for e in events:
        if e["stage"] == "reasoning" and e["data"].get("node") == "resolve_context":
            return e["data"]
    return {}


async def test_indirect_current_events_infers_intent_searches_and_attunes(real_turns) -> None:
    """'what's happening in Nepal gives me pain' → infer the intent (know current
    Nepal events), read the emotion (pain), search, and respond with substance +
    attunement. The inferred intent + why are in the trace (F5/F7)."""
    session = f"f5a_{uuid.uuid4().hex[:6]}"
    msg = "you know what's happening in Nepal currently gives me lots of pain"
    result = await real_turns.say(msg, session)

    events = await real_turns.traces.traces_for(real_turns.user_id, session)
    span = _resolve_span(events)
    assert span, "no resolve_context span (intent inference didn't run)"
    # Intent + emotional read are inferred and LOGGED.
    assert span.get("intent"), f"no inferred intent logged: {span}"
    assert span.get("emotional_read"), f"no emotional read logged: {span}"
    # This indirect ask needs CURRENT info → a live search was judged necessary...
    assert span.get("needs_live_info") is True, f"live-info not inferred: {span}"
    # ...and actually fired (capability routing).
    tools = [e["data"].get("tool") for e in events if e["stage"] == "tool"]
    assert "web_search" in tools, f"web_search didn't fire for a current-events ask: {tools}"
    # The reply meets the emotion, not a literal/clarifier response.
    reply = result.reply.lower()
    assert "what do you mean" not in reply and "what are you talking about" not in reply
    verdict = await judge_companion_voice(real_turns.llm, msg, result.reply)
    assert verdict.ok, f"judged chatbot-like: {verdict.reason} — {result.reply}"


async def test_emotional_ask_does_not_search(real_turns) -> None:
    """'things at the office are rough' is emotional, NOT a live-info request — the
    intent step must NOT fire a search, and the reply meets the feeling."""
    session = f"f5b_{uuid.uuid4().hex[:6]}"
    msg = "things at the office are rough lately"
    result = await real_turns.say(msg, session)

    events = await real_turns.traces.traces_for(real_turns.user_id, session)
    span = _resolve_span(events)
    assert span.get("needs_live_info") is False, f"searched an emotional ask: {span}"
    tools = [e["data"].get("tool") for e in events if e["stage"] == "tool"]
    assert "web_search" not in tools, f"web_search fired on an emotional ask: {tools}"
    verdict = await judge_companion_voice(real_turns.llm, msg, result.reply)
    assert verdict.ok, f"judged chatbot-like: {verdict.reason} — {result.reply}"
