"""E1 — enforcement and gate reachability. D-6, D-7, D-8, D-16 (fixed) and D-9 (open).

Every test here asserts what `docs/ai-companion-design-doc.md` §9.3 requires.
Those still marked `@pytest.mark.defect` are RED at HEAD and name their entry in
`docs/DEFECTS_FOUND.md`. The marker is deliberately not `xfail`: an `xfail(strict=False)` can
neither fail nor pass in a way anyone notices, which is how the last tone regression sat in
the "5 xpassed" column for months. Fix the defect, delete the marker.

    uv run pytest -m defect            # exactly what the engine still gets wrong
    uv run pytest -m "not defect"      # the green suite

**D-6 and D-8 shared one line of control flow.** `ResponseGenerator.generate()` returned
through `_finish()` directly — never `_finalize()` → `_apply_gates()` — on four paths: the
cost ceiling, a judgment JSON that failed validation twice, the plain-reply fallback, and a
total provider outage. `_apply_gates` is where the curiosity gate, `check_boundary()`,
`_warm_disclosure()` and self-reflection live, so the turns whose reply was least trustworthy
were the only ones nothing critiqued. Worse: on a live-info turn the "ships `last_draft`" path
shipped the model's ACKNOWLEDGEMENT ("I'll check that for you right now") as the final answer.

They now go through `_finish_gated()`, which derives a judgment and runs the same gates.

**D-7 and D-16.** `_enforce()` is the last thing every exit passes through, in `_finish` and
again at the end of `_apply_gates` — the second call because `_stream_reply` speaks the text
`_apply_gates` returns, and a reply enforced after it has been spoken is a companion that
audibly walks back its own words. Two absolute rules: a flagged draft never ships, and an
acknowledgement never ships when the turn owed the user an answer.

**D-9 is still open** and is the one `defect` test left in this file.
"""

import json
from typing import Any

import pytest

from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import Judgment, ResponseGenerator
from core.reasoning.self_model import SelfModel
from core.reasoning.style import find_forbidden
from tests.fakes import FakeDocStore, FakeLLM, FakeVectorStore

USER = "u_enf"


class _SpanLog:
    """Captures the reasoning spans the generator emits, so a test can ask whether a step
    RAN — not merely whether its effect happened to show up in the reply."""

    def __init__(self) -> None:
        self.spans: list[tuple[str, dict[str, Any]]] = []

    def log(self, level: str, event: str, **fields: Any) -> None:
        self.spans.append((event, fields))

    def stages(self) -> list[str]:
        return [event for event, _ in self.spans]


def _prompt(utterance: str, **kw: Any) -> AssembledPrompt:
    base: dict[str, Any] = dict(
        user_id=USER,
        session_id="enf",
        utterance=utterance,
        system_prompt="You are Companion.",
        messages=[
            {"role": "system", "content": "You are Companion."},
            {"role": "user", "content": utterance},
        ],
        complexity_hint="simple",
    )
    base.update(kw)
    return AssembledPrompt(**base)


def _generator(llm: FakeLLM, logs: _SpanLog) -> ResponseGenerator:
    from core.profile import ProfileService, TraitRegistry

    docs = FakeDocStore()
    profiles = ProfileService(docs)
    registry = TraitRegistry(docs, profiles)
    return ResponseGenerator(llm, SelfModel(docs, FakeVectorStore(), llm=None), registry, logs=logs)


def _turn(draft: str, **judgment: Any) -> str:
    body = {
        "intent_confidence": 0.9,
        "novelty_score": 0.1,
        "emotional_salience": 0.1,
        "ambiguity": 0.1,
        "complexity_tier": "simple",
        "requires_nature_disclosure": False,
        "capability_boundary_flag": None,
    }
    body.update(judgment)
    return json.dumps({"judgment": body, "tool_request": None, "draft_response": draft})


# ── the happy path really does run the gates (the control) ───────────────────


async def test_a_valid_turn_runs_self_reflection() -> None:
    """The control. If this ever goes red, the reproducers below prove nothing."""
    logs = _SpanLog()
    llm = FakeLLM([_turn("Hey, good to hear from you.")])
    await _generator(llm, logs).generate(_prompt("hi"))
    assert "reflection" in logs.stages()


# ── D-6: the gates are skipped on every fallback path ────────────────────────


async def test_self_reflection_runs_even_when_the_judgment_json_is_invalid() -> None:
    """D-6. Design §9.3: "Self-reflection is a first-class step, not a bolt-on" — it runs
    "before the reply goes out", every turn. Two bad JSON responses send `generate()`
    through `_plain_reply()` → `_finish()`, and no gate runs at all."""
    logs = _SpanLog()
    llm = FakeLLM(["not json", "still not json", "Hey — good to see you."])

    result = await _generator(llm, logs).generate(_prompt("hi"))

    assert result.final_text
    assert "reflection" in logs.stages(), (
        "the judgment JSON failed twice, the engine fell back to a plain reply, and "
        "self-reflection never ran. See DEFECTS_FOUND.md D-6."
    )


async def test_the_overclaim_guard_runs_even_on_a_fallback_reply() -> None:
    """D-6. `check_boundary()` (design §5.2) is the rule layer that rewrites "I understand
    exactly how you feel" before it reaches TTS. On the `_plain_reply` fallback it never
    executes, so a fallback turn can overclaim feeling — the one thing §1.4 forbids
    absolutely."""
    logs = _SpanLog()
    overclaim = "I understand exactly how you feel, I feel your pain too."
    llm = FakeLLM(["{bad", "{bad again", overclaim])

    result = await _generator(llm, logs).generate(_prompt("my dad died last week"))

    assert "i understand exactly how you feel" not in result.final_text.lower(), (
        f"an overclaiming fallback reply shipped unrewritten: {result.final_text!r}. "
        "See DEFECTS_FOUND.md D-6."
    )


# ── D-8: the acknowledgement survives as the final reply ─────────────────────


async def test_the_search_acknowledgement_never_becomes_the_final_reply() -> None:
    """D-8 (== SESSION_REPORT_GATE_RERUN §3.2(b)). When the model's second draft fails
    validation, `generate()` returns `last_draft` — which on a live-info turn is the
    holding line it emitted while starting the search. The user hears
    "I'll check that for you right now" and never gets an answer.

    `_needs_capability_repair()` already recognises that exact shape. Nothing consults it
    before shipping `last_draft`.
    """
    from core.reasoning.response_gen import _needs_capability_repair

    ack = "Oh, you're looking for the current price of OP again. I'll check that right now."
    assert _needs_capability_repair(ack), "the fixture is not a hollow promise; rewrite it"

    logs = _SpanLog()
    llm = FakeLLM([_turn(ack), "not json", "not json either"])

    result = await _generator(llm, logs).generate(_prompt("what's the price of OP?"))

    assert not _needs_capability_repair(result.final_text), (
        f"the engine shipped its own acknowledgement as the answer: {result.final_text!r}. "
        "See DEFECTS_FOUND.md D-8."
    )


async def test_the_streaming_path_never_speaks_an_unenforced_draft() -> None:
    """`_stream_reply` speaks the text `_apply_gates` returns, sentence by sentence, and only
    then calls `_finish`. Enforcing in `_finish` alone would mean the reply is corrected AFTER
    the user has heard it — a companion that audibly walks back its own words.

    So enforcement runs at the end of `_apply_gates` too. This asserts it there, at the exact
    point the voice path reads from.
    """
    gen = _generator(FakeLLM(["How may I assist you today?"]), _SpanLog())

    text, _action, caught = await gen._apply_gates(
        _prompt("hi"), "How can I help you today?", Judgment()
    )

    assert find_forbidden(text) == [], f"_apply_gates returned a flagged draft: {text!r}"
    assert caught == ["service-desk opener"], "enforcement did not report what it removed"


async def test_enforcement_leaves_a_clean_reply_exactly_as_it_was() -> None:
    """The invariant cuts one way only. A good reply passes through untouched — otherwise
    enforcement is a tax on every turn rather than a backstop on the bad ones."""
    good = "Oh Nandi, I am so sorry. Losing your dad is a lot to carry."
    gen = _generator(FakeLLM([]), _SpanLog())

    text, action, caught = await gen._apply_gates(_prompt("my dad died"), good, Judgment())

    assert text == good
    assert action == "respond"
    assert caught == [], "enforcement reported a catch on a clean reply"


# ── the fallback must not answer a volatile turn from training data ──────────


class _SearchDispatcher:
    """One `web_search` tool, recording every inline call."""

    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.inline_calls: list[Any] = []

    def tools_for(self, _context: Any) -> list[Any]:
        from core.tools.registry import ToolSpec

        return [ToolSpec(id="web_search", description="search the web", type="background")]

    async def run_inline(self, call: Any, _context: Any) -> Any:
        from core.tools.dispatcher import ToolResult

        self.inline_calls.append(call)
        return ToolResult(tool_id=call.tool_id, output=self.output, elapsed_ms=5.0)

    async def dispatch(self, *_a: Any, **_k: Any) -> Any:
        raise AssertionError("the fallback search must run INLINE")


async def test_a_volatile_turn_still_searches_when_the_judgment_json_breaks() -> None:
    """The fallback used to `return` before the capability backstop, so a JSON glitch on a
    volatile turn shipped the model's TRAINING-DATA answer.

    Observed live while fixing D-14: "who is the current prime minister of Nepal?" ->
    both judgment attempts returned malformed JSON -> the plain-reply fallback answered
    "Balendra Shah is still the Prime Minister", confidently, with zero searches. It happened
    to be right. Nothing in the engine knew that.
    """
    from core.tools.registry import ToolContext

    dispatcher = _SearchDispatcher({"found": True, "summary": "Sushila Karki is the current PM."})
    ctx = ToolContext(user_id=USER, session_id="enf")
    # `_build_search_query` makes no LLM call here: with no resolved entities and no user
    # context there is nothing to disambiguate the query against, so it uses the utterance.
    llm = FakeLLM(
        [
            "not json",  # judgment attempt 1
            "still not json",  # judgment attempt 2
            "Balendra Shah is still the Prime Minister.",  # _plain_reply, from training data
            "Right now it's Sushila Karki.",  # response_repair, from the search result
        ]
    )
    prompt = _prompt("who is the current prime minister of Nepal?", needs_live_info=True)

    result = await _generator(llm, _SpanLog()).generate(prompt, dispatcher, ctx)

    assert dispatcher.inline_calls, "a volatile turn answered from training data after a glitch"
    assert "Sushila Karki" in result.final_text


async def test_a_volatile_turn_whose_search_fails_says_so_rather_than_guessing() -> None:
    """§16. The reasoning step judged the answer would go stale without a lookup, and the
    lookup found nothing. Ship the honest line, never the model's stale draft."""
    from core.tools.registry import ToolContext

    dispatcher = _SearchDispatcher({"found": False, "summary": ""})
    ctx = ToolContext(user_id=USER, session_id="enf")
    llm = FakeLLM(["{bad", "{bad", "Balendra Shah is still the PM."])
    prompt = _prompt("who is the current prime minister of Nepal?", needs_live_info=True)

    result = await _generator(llm, _SpanLog()).generate(prompt, dispatcher, ctx)

    assert "Balendra Shah" not in result.final_text
    assert result.final_text.strip()


# ── D-7: the detector detects; nothing enforces ──────────────────────────────


async def test_a_draft_carrying_style_flags_never_becomes_the_final_reply() -> None:
    """D-7 (== SESSION_REPORT_GATE_RERUN §3.2(a)). `_finish()` computes `style_flags` on the
    text it is about to return, logs a warning — and returns it anyway. A `GenerationResult`
    with non-empty `style_flags` is, by construction, a reply the engine itself judged to be
    assistant-speak. It must never be the one the user hears.

    Here the self-reflection rewrite is given no way to succeed (the FakeLLM keeps returning
    banned phrasing), which is exactly the "repair loop exhausts" case the gate re-run hit.
    """
    logs = _SpanLog()
    banned = "How can I help you today?"
    llm = FakeLLM([_turn(banned), banned, banned, banned])

    result = await _generator(llm, logs).generate(_prompt("hi"))

    assert find_forbidden(result.final_text) == [], (
        f"the engine shipped a reply the detector flags: {result.final_text!r}. "
        "The detector detects; nothing enforces. See DEFECTS_FOUND.md D-7."
    )
    # And it says so: `style_flags` records what enforcement had to remove.
    assert result.style_flags, "enforcement fired but reported nothing to the trace"


# ── D-9: an exception on the reply path must never yield silence ─────────────


# ── D-9: an exception on the reply path must never yield silence ─────────────


class _AlwaysDown:
    """A provider that fails every call with the given exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def complete(self, *a: Any, **k: Any) -> Any:
        raise self._exc

    async def stream(self, *a: Any, **k: Any) -> Any:
        raise self._exc
        yield ""  # pragma: no cover — makes this an async generator


async def test_a_wrapped_provider_outage_degrades_to_an_honest_reply() -> None:
    """PASSES. When the port's contract is honoured — every provider failure surfaces as
    `LLMUnavailable` — the engine degrades correctly and the user still hears words.

    This is the control for the defect below: the fallback machinery works; it is the set
    of exceptions that reaches it which is too narrow.
    """
    from ports.llm import LLMUnavailable

    result = await _generator(_AlwaysDown(LLMUnavailable("read timeout")), _SpanLog()).generate(
        _prompt("what's happening in Nepal?")
    )
    assert result.final_text.strip(), "the engine returned an EMPTY reply — the user heard silence"
    assert len(result.final_text.split()) >= 4, f"barely a reply: {result.final_text!r}"
    # A TOTAL model outage must be HONEST that the system is down — never the warm "I'm right
    # here with you, what's going on?" line, which pretends to be engaged when nothing is
    # thinking (user report: the credit-exhausted reply was misleading).
    low = result.final_text.lower()
    assert "right here with you" not in low, f"pretended to be present during an outage: {low!r}"
    assert any(w in low for w in ("down", "try", "unavailable", "can't think")), (
        f"outage reply didn't signal a system problem: {result.final_text!r}"
    )


@pytest.mark.defect
async def test_any_reply_path_exception_yields_a_reply_never_a_raise() -> None:
    """D-9 (== SESSION_REPORT_GATE_RERUN §3.1: `reply=""`, `first_audio=None`, the user
    heard SILENCE).

    The engine only degrades on `LLMUnavailable`. A dependency failure that arrives as
    anything else — `httpx.ReadTimeout`, `openai.APIError` — escapes the whole turn.
    `_build_search_query`, `_warm_disclosure` and `_rewrite_assistant_speak` each catch
    `LLMUnavailable` and nothing wider, and `OpenRouterLLM.stream()` guards only the
    creation of the stream, not the `async for` that consumes it.

    `RuntimeError` is used here because it is a dependency-shaped failure, NOT one of
    `core.errors.PROGRAMMING_ERRORS` — those must keep re-raising loudly (F3), and this
    test must not be read as asking for a blanket `except Exception`.
    """
    from core.errors import PROGRAMMING_ERRORS

    boom = RuntimeError("ReadTimeout")
    assert not isinstance(boom, PROGRAMMING_ERRORS), "this fixture must be a dependency failure"

    result = await _generator(_AlwaysDown(boom), _SpanLog()).generate(_prompt("what's happening?"))

    assert result.final_text.strip(), "the engine returned an EMPTY reply"


async def test_a_programming_error_still_fails_loudly() -> None:
    """The guard on the fix for D-9. Widening the reply path's `except` must NOT swallow
    our own bugs — that is exactly the disease `core/errors.py` was written to cure, and
    the `TypeError` it hid silenced every voice turn for months.

    Passes today (nothing catches it). It exists so that fixing D-9 cannot regress F3.
    """
    with pytest.raises(TypeError):
        await _generator(_AlwaysDown(TypeError("wrong arity")), _SpanLog()).generate(_prompt("hi"))
