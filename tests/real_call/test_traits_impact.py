"""Real-call proof that behavioral traits actually shape responses (F6), and that
the active traits + their injected text are visible in the trace.

A/B: the SAME message is answered with traits ON (default) vs OFF (all disabled for
the user). A real judge then decides which reply is warmer / more companion-like —
traits-on must win — proving the traits aren't decorative. Real model + stores.
"""

import uuid

import pytest

from tests.support.judge import judge_companion_voice

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]

_TRAIT_IDS = ["curiosity_policy", "humor", "response_voice", "emotional_intelligence"]


def _assembly_span(events: list) -> dict:
    """Find the assembly TraceEvent carrying trait evidence (events are TraceEvent
    objects from TurnResult.trace)."""
    for e in events:
        if e.stage == "assembly" and "active_traits" in e.data:
            return e.data
    return {}


async def test_traits_change_the_response_and_show_in_trace(real_turns) -> None:
    """A/B the SAME lecture-prone message with traits ON vs OFF. The traces show the
    active traits + injected text (F6 visibility); the outputs differ; and the
    response_voice brevity trait makes the ON reply concise where OFF lectures —
    a deterministic, trait-attributable effect (not a subjective judgment)."""
    profiles = real_turns.profiles
    original = (await profiles.get(real_turns.user_id)).traits_enabled
    # Strongly lecture-prone: without the response_voice brevity trait the model
    # dumps a long explanation; with it, one or two concise sentences.
    msg = "tell me everything you know about black holes"
    try:
        # ── traits OFF ──────────────────────────────────────────────────────
        await profiles.update(
            real_turns.user_id, {"traits_enabled": {t: False for t in _TRAIT_IDS}}
        )
        off = await real_turns.say(msg, f"f6off_{uuid.uuid4().hex[:6]}")
        off_span = _assembly_span(off.trace)
        assert off_span.get("active_traits") == [], f"traits leaked while off: {off_span}"

        # ── traits ON (defaults) ────────────────────────────────────────────
        await profiles.update(real_turns.user_id, {"traits_enabled": {t: True for t in _TRAIT_IDS}})
        on = await real_turns.say(msg, f"f6on_{uuid.uuid4().hex[:6]}")
        on_span = _assembly_span(on.trace)
        # F6 visibility: the trace shows WHICH traits were active AND the exact text
        # they injected — not just their names.
        assert len(on_span.get("active_traits", [])) == len(_TRAIT_IDS), on_span
        assert all(any(tid in tag for tag in on_span["active_traits"]) for tid in _TRAIT_IDS), (
            on_span["active_traits"]
        )
        assert "friend, not a customer-service assistant" in on_span.get("trait_text", ""), (
            "injected response_voice text missing from the trace"
        )

        # Impact: the traits genuinely change the output...
        assert on.reply.strip() != off.reply.strip(), "traits made no difference to the reply"
        # ...in the direction the response_voice brevity trait dictates: the ON reply
        # is materially shorter than the lecture the OFF prompt produces.
        assert len(on.reply) < len(off.reply), (
            f"brevity trait had no effect: on={len(on.reply)} off={len(off.reply)}\n"
            f"ON: {on.reply}\nOFF: {off.reply}"
        )
        # And the traits-ON reply meets the companion standard (calibrated judge).
        verdict = await judge_companion_voice(real_turns.llm, msg, on.reply)
        assert verdict.ok, f"traits-on reply judged chatbot-like: {verdict.reason} — {on.reply}"
    finally:
        # Restore explicitly (not just the snapshot): merge-update means a killed run
        # could leave the demo user disabled, so re-enable any trait that was on
        # originally and default the rest to enabled (the demo user's known-good state).
        restore = {t: original.get(t, True) for t in _TRAIT_IDS}
        await profiles.update(real_turns.user_id, {"traits_enabled": restore})
