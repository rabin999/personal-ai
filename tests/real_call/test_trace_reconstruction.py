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
