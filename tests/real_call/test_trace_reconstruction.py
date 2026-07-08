"""Real-call trace completeness (plan Item 6 / §3): a real turn must be fully
reconstructable from the persisted trace alone — every pipeline stage, the model
calls with tokens/cost/latency, the tool envelope, self-reflection, the response
(+ raw voice_text), and a per-turn totals roll-up.
"""

import uuid

import pytest

from api.routes.debug import _turn_totals

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]


async def test_a_turn_is_fully_reconstructable_from_the_trace(real_turns) -> None:
    session = f"trace_{uuid.uuid4().hex[:6]}"
    # A live-info turn exercises the richest pipeline (tools + escalation).
    result = await real_turns.say("what's the weather in Kathmandu right now?", session)
    assert result.reply

    events = await real_turns.traces.traces_for("u_demo_001", session)
    stages = [e["stage"] for e in events]

    # Every core §3 stage is present in the durable trace.
    for required in ("session", "retrieval", "assembly", "router", "llm", "generation", "response"):
        assert required in stages, f"trace missing stage {required!r}; have {sorted(set(stages))}"

    # Self-reflection ran and is its own span (§3.8).
    assert "reflection" in stages, "self-reflection span missing from trace"

    # At least one model call carries tokens + cost + latency + model.
    llm_spans = [e for e in events if e["stage"] == "llm"]
    assert llm_spans, "no llm.call spans persisted"
    rich = [
        e
        for e in llm_spans
        if (e["data"].get("input_tokens") or e["data"].get("tokens_in"))
        and e["data"].get("model")
        and e["data"].get("latency_ms") is not None
    ]
    assert rich, f"llm spans lack tokens/model/latency: {[e['data'] for e in llm_spans]}"

    # The response span keeps the CLEAN text and the raw tagged voice_text (§1.4).
    response_spans = [e for e in events if e["stage"] == "response"]
    assert response_spans and "voice_text" in response_spans[-1]["data"]

    # Item 7: the assembly span records the prompt_version that produced the turn.
    assembly = [e for e in events if e["stage"] == "assembly"]
    assert assembly and str(assembly[-1]["data"].get("prompt_version", "")).startswith("pt")

    # Item 7: llm spans carry prompt-cache hit/miss visibility.
    assert all("cache_hit" in e["data"] for e in llm_spans)

    # Per-turn totals roll up cost + tokens from the spans (§3.12).
    totals = _turn_totals(events)
    assert totals, "no per-turn totals computed"
    turn = totals[0]
    assert turn["llm_calls"] >= 1
    assert turn["tokens_in"] > 0 and turn["cost_usd"] > 0.0
    assert turn["reflected"] is True

    # ── F7: everything needed to evaluate the turn is in the trace, VERBATIM ──
    adata = assembly[-1]["data"]
    # The REAL assembled prompt (full, not a 4k summary) + the actual messages sent.
    assert len(adata.get("system_prompt", "")) > 4000, "system prompt not stored verbatim"
    assert isinstance(adata.get("messages"), list) and adata["messages"], "messages array missing"
    assert adata["messages"][-1]["content"] == "what's the weather in Kathmandu right now?"
    # F6: the active traits + injected trait text are in the trace.
    assert adata.get("active_traits") and adata.get("trait_text")
    # F5: the inferred intent + emotional read + live-info decision are logged.
    resolve = [
        e
        for e in events
        if e["stage"] == "reasoning" and e["data"].get("node") == "resolve_context"
    ]
    assert resolve and resolve[0]["data"].get("intent"), "inferred intent not in trace"
    # F7: self-reflection shows the actual draft → critique → (revision), not just a bool.
    reflection = [e for e in events if e["stage"] == "reflection"]
    assert reflection, "no reflection span"
    rdata = reflection[-1]["data"]
    assert "draft" in rdata and "critique" in rdata, f"reflection lacks draft/critique: {rdata}"
    # A tool turn records the fetched data (search result) + the why-not for unused tools.
    tool_spans = [e for e in events if e["stage"] == "tool"]
    assert tool_spans and tool_spans[-1]["data"].get("result"), "tool result not captured"
