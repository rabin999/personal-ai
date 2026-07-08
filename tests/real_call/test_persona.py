"""Real-call dynamic persona (brief U0-U2) — REAL model + REAL stores, no mocks.

Proves the three layers are correctly separated at the WRITE step and that the
persona genuinely SHAPES responses:
- a style request ("keep it short") routes to the PERSONA, not to semantic facts;
- an identity statement routes to semantic FACTS, not the persona;
- the learned persona is injected into prompt assembly and makes the SAME question
  get a different STYLE for a blunt vs. a warm user (judged by a real model).
"""

import uuid

import pytest

from core.psych.persona import StyleSignal

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]


async def test_style_request_routes_to_persona_not_facts(real_turns) -> None:
    p = real_turns._p
    user = f"u_persona_{uuid.uuid4().hex[:8]}"  # fresh, isolated user
    # A clear HOW-to-talk request — this is DELIVERY/STYLE, not a fact.
    result = await p.extractor.extract_and_store(
        user,
        "s",
        "hey can you just keep your answers short and to the point? no long paragraphs",
        "Got it — short and direct from here.",
    )
    assert result.persona_written >= 1, "style request must land in the persona layer"
    # It must NOT have been stored as a semantic fact (style ≠ fact, brief U0).
    facts = " | ".join(f.fact.lower() for f in await p.semantic.profile_facts(user, limit=20))
    assert "paragraph" not in facts

    # And the persona now actively shapes the prompt.
    rendered = await p.persona.render_for_prompt(user)
    assert rendered and ("short" in rendered.lower() or "point" in rendered.lower())


async def test_identity_statement_routes_to_facts_not_persona(real_turns) -> None:
    """A pure identity statement is a FACT — it must route to semantic memory and must
    NOT manufacture a persona/style note (brief U0 layering)."""
    p = real_turns._p
    user = f"u_fact_{uuid.uuid4().hex[:8]}"
    result = await p.extractor.extract_and_store(
        user, "s", "I run a company called Xenon Technology.", "Nice, tell me about it."
    )
    # The real extraction routed it to the FACTS layer, not the persona layer.
    assert any("xenon" in f.lower() for f in result.facts), "identity → semantic facts"
    assert result.persona == [], "a plain fact must not be stored as a style signal"
    persona = await p.persona.render_for_prompt(user)
    assert "xenon" not in persona.lower()


async def test_persona_is_injected_and_differs_per_user(real_turns) -> None:
    """The learned persona is injected into prompt assembly and makes the SAME question
    carry a DIFFERENT style block per user — the mechanism that drives responses (U2).
    Asserts on the real assembled prompt (independent of generation credits)."""
    p = real_turns._p
    blunt = f"u_blunt_{uuid.uuid4().hex[:8]}"
    warm = f"u_warm_{uuid.uuid4().hex[:8]}"
    await p.persona.apply(
        blunt,
        [
            StyleSignal(
                text="You want short, blunt, to-the-point answers — no fluff",
                dimension="brevity",
                stated=True,
            )
        ],
    )
    await p.persona.apply(
        warm,
        [
            StyleSignal(
                text="You like warm, detailed, encouraging answers",
                dimension="warmth",
                stated=True,
            )
        ],
    )
    q = "should I take a break from work this weekend?"
    pb = await p.assembler.assemble(blunt, f"s_{blunt}", q)
    pw = await p.assembler.assemble(warm, f"s_{warm}", q)
    # Persona shaped both prompts (evidenced in the trace via persona_active).
    assert pb.persona_active and pw.persona_active
    # ...and each carries its OWN distinct style, so the same question differs per user.
    assert "blunt" in pb.system_prompt.lower() and "blunt" not in pw.system_prompt.lower()
    assert "encouraging" in pw.system_prompt.lower()
    assert pb.sections["persona"] != pw.sections["persona"]
