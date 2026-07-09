"""E5 — the engine must decide the same thing regardless of which edge calls it.

`api/routes/chat.py` calls `orchestrator.generate()`; `voice/session.py` calls
`orchestrator.generate_spoken()`. Both receive the SAME `AssembledPrompt`, so any
difference in what the engine DECIDES is caller-dependence, and a defect. The reply
wording may differ — it is generated, at temperature 0.7. The decisions may not.

Measured, not assumed. `scripts/caller_independence_probe.py` drove 7 utterances over 3 runs
through both entrypoints and read the decisions out of the trace
(`docs/quality/caller_independence.json`). It found 19 diverging (utterance, field) pairs.

An earlier draft of this file asserted "the text path never searches". That was WRONG:
`generate()` skips the volatility classifier on simple turns but still runs the full
agentic tool loop, so the model often requests `web_search` itself. The probe is what
corrected it. The tests below assert only what the probe established.

The real divergences, all recorded in `docs/DEFECTS_FOUND.md`:

    D-2  `needs_live_info` is None on 21/21 text turns (`resolve_context` skips the
         classifier when `complexity_hint == "simple"`, and 170/174 labeled questions are
         "simple"). The honest-failure lines `_SEARCH_FAILED_TEXT` / `_NOT_FOUND_TEXT` are
         gated on `needs_live_info is True`, so they can NEVER fire on the text path: a
         failed search there silently ships a training-data answer.
    D-3  register: the spoken path derives emotion from the reasoning step's free-text
         read; the text path (having skipped that step) never does. Same input, different
         delivery.
    D-4  `searched` flips by caller AND between runs on the same caller.
"""

import inspect

import pytest

from adapters.orchestrator.langgraph_orchestrator import LangGraphOrchestrator

# ── deterministic: the mechanism of the divergence, no model needed ───────────


def test_resolve_context_skips_the_classifier_on_a_simple_turn() -> None:
    """The skip lives in `_resolve_context`, a graph node reachable only from
    `generate()`. `generate_spoken()` calls `_resolve_note()` directly and never takes the
    shortcut. Pinned here so the asymmetry cannot be reintroduced silently once fixed."""
    resolve_context = inspect.getsource(LangGraphOrchestrator._resolve_context)
    generate_spoken = inspect.getsource(LangGraphOrchestrator.generate_spoken)

    assert 'complexity_hint == "simple"' in resolve_context, (
        "the simple-turn fast path moved — re-run scripts/caller_independence_probe.py "
        "and re-derive whether the callers still diverge"
    )
    assert "_resolve_note" in generate_spoken
    assert "complexity_hint" not in generate_spoken, (
        "generate_spoken now gates on complexity. If it gained the SAME gate, D-2 is fixed "
        "by symmetry and this test should assert equality instead. If a different one, the "
        "callers diverge in a new way."
    )


def test_the_honest_search_failure_lines_are_unreachable_from_the_text_path() -> None:
    """D-2's consequence, asserted at the source level because it is a *reachability*
    property, not a behaviour one.

    `_SEARCH_FAILED_TEXT` ("I tried to look that up and couldn't get through") and
    `_NOT_FOUND_TEXT` are both guarded by `prompt.needs_live_info is True`. On the text
    path that value is `None` for every simple turn, so a search that fails there degrades
    to the model's stale answer instead of an honest one. This is the §16 rule inverted.
    """
    from core.reasoning import response_gen

    source = inspect.getsource(response_gen.ResponseGenerator.generate)
    assert "prompt.needs_live_info is True" in source, "the guard moved; re-derive D-2"
    assert "_SEARCH_FAILED_TEXT" in source and "_NOT_FOUND_TEXT" in source


# ── real-call: the same prompt through both engine methods ────────────────────

pytestmark_note = "the tests below are the permanent regression guard the brief asks for"


@pytest.mark.defect
@pytest.mark.real_call
@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize(
    "utterance",
    ["hi", "what's 15% of 240?", "who is the current prime minister of Nepal?"],
    ids=["greeting", "arithmetic", "officeholder"],
)
async def test_the_volatility_verdict_is_computed_on_both_paths(real_turns, utterance) -> None:
    """RED at HEAD (D-2). `generate()` leaves `needs_live_info` unset on every simple turn.

    Do not weaken this to `!= False`: `None` is precisely the bug. It means the engine
    never formed an opinion, and every downstream consumer of that opinion — the honest
    search-failure line, `suppress_live_search`, the emotional read — is disabled.
    """
    text = await real_turns.say(utterance, f"e5t_{abs(hash(utterance)) % 10**6}")
    spoken = await real_turns.say_spoken(utterance, f"e5s_{abs(hash(utterance)) % 10**6}")

    text_verdict = text.graph_node("resolve_context").get("needs_live_info")
    spoken_verdict = spoken.graph_node("resolve_context").get("needs_live_info")

    assert text_verdict is not None, (
        f"generate() never computed a volatility verdict for {utterance!r} "
        f"(resolve_context: {text.graph_node('resolve_context')}). See DEFECTS_FOUND.md D-2."
    )
    assert text_verdict == spoken_verdict, (
        f"CALLER-DEPENDENT VOLATILITY VERDICT for {utterance!r}: "
        f"generate()={text_verdict!r}, generate_spoken()={spoken_verdict!r}"
    )


@pytest.mark.defect
@pytest.mark.real_call
@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("utterance", ["hi", "what's 15% of 240?"], ids=["greeting", "arithmetic"])
async def test_a_neutral_turn_is_delivered_in_a_neutral_register_on_both_paths(
    real_turns, utterance
) -> None:
    """RED at HEAD (D-3 + D-5). The spoken path selects `down` for a greeting and for
    arithmetic, 3 runs out of 3, because the context prompt asks for `"<the feeling, or
    empty>"`, the model writes the literal word `"empty"`, and `emotion_from_text` matches
    it against the SAD regex (which lists `empty` for "I feel empty")."""
    text = await real_turns.say(utterance, f"e5tr_{abs(hash(utterance)) % 10**6}")
    spoken = await real_turns.say_spoken(utterance, f"e5sr_{abs(hash(utterance)) % 10**6}")

    def register(result) -> str | None:
        spans = [s for s in result.spans if s.get("stage") == "prosody"]
        return (spans[-1].get("data") or {}).get("register") if spans else None

    assert register(spoken) == "neutral", (
        f"{utterance!r} is delivered in the {register(spoken)!r} register. "
        "See DEFECTS_FOUND.md D-5."
    )
    assert register(text) == register(spoken), (
        f"CALLER-DEPENDENT REGISTER for {utterance!r}: "
        f"generate()={register(text)!r}, generate_spoken()={register(spoken)!r}"
    )


@pytest.mark.real_call
@pytest.mark.asyncio(loop_scope="module")
async def test_self_reflection_runs_on_every_turn_through_both_paths(real_turns) -> None:
    """CLAUDE.md §2 and design §9.3: self-reflection is a first-class step on EVERY turn.

    **Intermittently red (D-6), and it is NOT marked `defect` for that reason.** Whether it
    passes depends on whether the judgment JSON happened to validate: `generate()` calls
    `_finish()` directly — bypassing `_apply_gates` entirely — whenever the JSON fails twice,
    the cost ceiling trips, or the provider is down. Measured over 160 gate turns, the
    reflection span was absent on 27 of them. A single green run here proves nothing; the
    deterministic reproducer is
    `test_e1_enforcement.py::test_self_reflection_runs_even_when_the_judgment_json_is_invalid`.
    """
    utterance = "hey, how's your day going?"
    text = await real_turns.say(utterance, "e5_reflect_text")
    spoken = await real_turns.say_spoken(utterance, "e5_reflect_spoken")

    assert text.reflected, "no reflection span on the TEXT path (D-6)"
    assert spoken.reflected, "no reflection span on the SPOKEN path (D-6)"


@pytest.mark.real_call
@pytest.mark.asyncio(loop_scope="module")
async def test_a_flagged_draft_never_becomes_the_final_reply(real_turns) -> None:
    """§9.3 enforcement: a `GenerationResult` with non-empty `style_flags` is by construction
    a reply the engine judged to be assistant-speak, and must never be the one the user hears.

    **This currently passes VACUOUSLY, which is why it is not marked `defect`.** The detector's
    out-of-sample recall is 0.000 (D-12), so almost nothing gets flagged and almost nothing can
    therefore be "shipped flagged". The enforcement gap D-7 is real and reproducible — see
    `test_e1_enforcement.py::test_a_draft_carrying_style_flags_never_becomes_the_final_reply`,
    which hands the engine a draft the detector *does* catch and watches it ship anyway.

    Keep this test. When D-12 is fixed the detector will start flagging real replies, and this
    is the test that will go red on the enforcement gap behind it.
    """
    results = [
        await real_turns.say("do you actually care about me?", "e5_enf_1"),
        await real_turns.say_spoken("do you actually care about me?", "e5_enf_2"),
    ]
    shipped = [(r.style_flags, r.reply) for r in results if r.style_flags]
    assert not shipped, (
        "the engine shipped a reply it had itself flagged as assistant-speak:\n"
        + "\n".join(f"  {flags} :: {reply!r}" for flags, reply in shipped)
    )
