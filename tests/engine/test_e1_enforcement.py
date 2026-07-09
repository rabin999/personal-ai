"""E1 — enforcement and gate reachability. Reproducers for known, unfixed defects.

Every test here asserts what `docs/ai-companion-design-doc.md` and `CLAUDE.md §2` require.
They are RED at HEAD. Each names its defect in `docs/DEFECTS_FOUND.md`. They are marked
`@pytest.mark.defect`, not `xfail`: an `xfail(strict=False)` can neither fail nor pass in a
way anyone notices, which is precisely how the last tone regression sat in the "5 xpassed"
column for months. Fix the defect, delete the marker.

    uv run pytest -m defect            # see exactly what the engine gets wrong
    uv run pytest -m "not defect"      # the green suite

The root cause of D-6 and D-8 is one line of control flow. `ResponseGenerator.generate()`
returns through `_finish()` directly — never `_finalize()` → `_apply_gates()` — on four
paths:

    response_gen.py:468   cost ceiling tripped with no draft   → _SAFE_FALLBACK_TEXT
    response_gen.py:475   judgment JSON invalid twice          → ships `last_draft`
    response_gen.py:481   plain-reply fallback                 → ships `plain`
    response_gen.py:484   provider fully down                  → _SAFE_FALLBACK_TEXT

`_apply_gates` is where the curiosity gate, `check_boundary()`, `_warm_disclosure()` and
self-reflection live. So on exactly the turns whose reply is least trustworthy — a fallback
— the companion's entire character machinery is skipped. Line 475 is worse than that: on a
live-info turn `last_draft` is the model's ACKNOWLEDGEMENT ("I'll check that for you right
now"), so a JSON glitch ships the ack as the final answer, carrying no answer at all. That
is the `live_search` failure in SESSION_REPORT_GATE_RERUN §3.2(b), and its cause.
"""

import json
from typing import Any

import pytest

from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import ResponseGenerator
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


@pytest.mark.defect
async def test_self_reflection_runs_even_when_the_judgment_json_is_invalid() -> None:
    """D-6. CLAUDE.md §2: "Self-reflection is a first-class step, not a bolt-on" — it runs
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


@pytest.mark.defect
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


@pytest.mark.defect
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


# ── D-7: the detector detects; nothing enforces ──────────────────────────────


@pytest.mark.defect
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

    assert result.style_flags == [], (
        f"the engine shipped a reply it had flagged {result.style_flags} as assistant-speak: "
        f"{result.final_text!r}. The detector detects; nothing enforces. "
        "See DEFECTS_FOUND.md D-7."
    )
    assert find_forbidden(result.final_text) == []


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
