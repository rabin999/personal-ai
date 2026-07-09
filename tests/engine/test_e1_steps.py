"""E1 — the engine steps that nothing tested.

Every test here exists because `scripts/mutation_audit.py` proved the behaviour it
covers could be deleted from the engine without a single one of 653 tests noticing.
The six SURVIVED mutations, and the test that now kills each:

    live_lookup_always_false       → test_volatile_verdict_routes_to_the_agentic_path
    capability_repair_disabled     → test_capability_repair_forces_a_real_search…
    search_query_is_raw_utterance  → test_search_query_is_built_from_the_resolved_entity
    warm_disclosure_disabled       → test_warm_disclosure_polishes_a_cold_nature_answer
    degenerate_rewrite_accepted    → test_a_rewrite_that_guts_the_reply_is_rejected
    query_echo_not_stripped        → test_the_search_query_is_never_spoken_back

Four of those six are the fixes shipped by the previous two sessions (S1, S2, S4).
They had zero coverage; `_requires_live_lookup`, `_build_search_query`,
`_strip_query_echo`, `_warm_disclosure` and `_is_degenerate_rewrite` were referenced
by no test in the repository.

Where a step is deterministic, it is asserted directly. Where a step calls the model,
the assertion is on what the engine SENDS and what it does with what comes back — the
FakeLLM is the step's input, never the thing under test.
"""

from typing import Any

import pytest

from core.memory.entities import EntityCandidate
from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import (
    Judgment,
    ResponseGenerator,
    _is_degenerate_rewrite,
    _requires_live_lookup,
    _strip_query_echo,
)
from core.reasoning.self_model import SelfModel
from core.tools.dispatcher import ToolCall, ToolResult
from core.tools.registry import ToolContext, ToolSpec
from tests.fakes import FakeDocStore, FakeLLM, FakeVectorStore

USER = "u_e1"


def _prompt(utterance: str, **kw: Any) -> AssembledPrompt:
    base: dict[str, Any] = dict(
        user_id=USER,
        session_id="e1",
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


def _generator(llm: FakeLLM) -> ResponseGenerator:
    docs = FakeDocStore()
    from core.profile import ProfileService, TraitRegistry

    profiles = ProfileService(docs)
    registry = TraitRegistry(docs, profiles)
    return ResponseGenerator(llm, SelfModel(docs, FakeVectorStore(), llm=None), registry)


# ── volatility → routing ──────────────────────────────────────────────────────


def test_volatile_verdict_routes_to_the_agentic_path() -> None:
    """S1. The reasoning step's `needs_live_info=True` is the PRIMARY gate. When it
    stopped being honoured, `generate_spoken` would stream a training-data answer and
    never reach a tool — the "who is the prime minister of Nepal" class of failure."""
    assert _requires_live_lookup(_prompt("anything at all", needs_live_info=True))


def test_an_unknown_verdict_falls_back_to_the_deterministic_backstop() -> None:
    """`None` means the classifier gave no usable answer — never "don't search"."""
    assert _requires_live_lookup(_prompt("who is the current prime minister of Nepal?"))
    assert _requires_live_lookup(_prompt("is Tim Cook still the CEO of Apple?"))


def test_a_stable_turn_with_no_verdict_does_not_search() -> None:
    """Over-searching is its own failure: a needless search costs a second AND breaks
    the conversational feel of a turn that needed no facts at all."""
    assert not _requires_live_lookup(_prompt("what's 15% of 240?"))
    assert not _requires_live_lookup(_prompt("I'm feeling kind of low today"))
    assert not _requires_live_lookup(_prompt("do you actually care about me?"))


# ── search query construction ─────────────────────────────────────────────────


async def test_search_query_is_built_from_the_resolved_entity() -> None:
    """S2. `"what's the LTP of OP?"` sent verbatim to Serper returns the price of the
    *Optimism crypto token*, even when `OP` was correctly resolved to a NEPSE share in
    the user's portfolio. The resolved entity must reach the query builder."""
    llm = FakeLLM(["OP NEPSE share last traded price Nepal Stock Exchange"])
    gen = _generator(llm)
    prompt = _prompt(
        "what's the LTP of OP?",
        resolved_entities=[
            EntityCandidate(
                entity_id="proj_folio",
                entity_type="project",
                name="OP",
                score=0.9,
                description="OP — a NEPSE-listed share, bought at NPR 300",
            )
        ],
        sections={"entities": "OP — a NEPSE-listed share held in the user's portfolio"},
    )

    query = await gen._build_search_query(prompt)

    assert query != prompt.utterance, "the RAW utterance was sent to the search engine"
    sent = "\n".join(
        m["content"] for call in llm.calls for m in call["messages"] if m["role"] == "user"
    )
    assert "RESOLVED ENTITIES" in sent and "OP (project)" in sent, (
        f"the resolved entity never reached the query builder:\n{sent}"
    )
    assert "NEPSE" in sent, "the user's own context was not used to disambiguate 'OP'"
    assert "NEPSE" in query


async def test_search_query_falls_back_to_the_raw_utterance_without_context() -> None:
    """No entities and no user context → nothing to disambiguate with; don't pay for a
    query-building call. This is the branch the mutation replaced wholesale."""
    llm = FakeLLM([])
    query = await _generator(llm)._build_search_query(_prompt("what's the weather?"))
    assert query == "what's the weather?"
    assert llm.calls == [], "spent an LLM call with nothing to disambiguate against"


def test_the_search_query_is_never_spoken_back() -> None:
    """The model writes the query into its own draft: "I'll check that for you right
    now. OP NEPSE LTP current price The current LTP of OP is NPR 308.90." The user must
    never hear the query."""
    query = "OP NEPSE LTP current price Nepal stock exchange"
    draft = f"I'll check that right now. {query} The current LTP of OP is NPR 308.90."
    spoken = _strip_query_echo(draft, query)
    assert query not in spoken
    assert "308.90" in spoken, "stripping the echo destroyed the answer"


def test_strip_query_echo_leaves_an_unrelated_reply_alone() -> None:
    text = "The current LTP of OP is NPR 308.90."
    assert _strip_query_echo(text, "some other query") == text


# ── capability repair (the forced search backstop) ────────────────────────────


class _SearchDispatcher:
    """Minimal §13 dispatcher exposing one `web_search` tool. Records every call so a
    test can assert the backstop actually reached the tool layer."""

    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.inline_calls: list[ToolCall] = []

    def tools_for(self, context: ToolContext) -> list[ToolSpec]:
        return [
            ToolSpec(
                id="web_search",
                description="search the live web",
                type="background",
                latency_class="slow",
            )
        ]

    async def run_inline(self, call: ToolCall, context: ToolContext) -> ToolResult:
        self.inline_calls.append(call)
        return ToolResult(tool_id=call.tool_id, output=self.output, elapsed_ms=12.0)

    async def dispatch(self, call: ToolCall, context: ToolContext, *, confirmed: bool = False):
        raise AssertionError("capability repair must run INLINE, not enqueue")


async def test_capability_repair_forces_a_real_search_and_answers_from_it() -> None:
    """§8.8. The model refused ("I don't have access to real-time data") and ran no
    tool. The backstop must run a real `web_search` and re-answer from the result —
    never ship the refusal."""
    llm = FakeLLM(
        [
            "current prime minister of Nepal",  # _build_search_query
            "Right now it's Sushila Karki — she took over recently.",  # response_repair
        ]
    )
    dispatcher = _SearchDispatcher(
        {"found": True, "summary": "Sushila Karki is the current PM of Nepal."}
    )
    ctx = ToolContext(user_id=USER, session_id="e1")
    prompt = _prompt(
        "who is the current prime minister of Nepal?",
        resolved_entities=[
            EntityCandidate(entity_id="e1", entity_type="topic", name="Nepal", score=0.8)
        ],
        sections={"facts": "the user lives in Kathmandu"},
    )

    answer = await _generator(llm)._capability_repair(prompt, dispatcher, ctx)

    assert dispatcher.inline_calls, "the backstop never reached web_search"
    assert dispatcher.inline_calls[0].tool_id == "web_search"
    assert answer and "Sushila Karki" in answer
    assert "don't have access" not in (answer or "")


async def test_capability_repair_gives_up_honestly_when_the_search_finds_nothing() -> None:
    """A search that returns `found=False` must NOT be dressed up as an answer —
    the caller then says so honestly (§16). Fabricating here is the worst failure."""
    llm = FakeLLM(["nepal prime minister"])
    dispatcher = _SearchDispatcher({"found": False, "summary": ""})
    ctx = ToolContext(user_id=USER, session_id="e1")
    prompt = _prompt("who is the prime minister?", sections={"facts": "user in Kathmandu"})

    assert await _generator(llm)._capability_repair(prompt, dispatcher, ctx) is None


async def test_capability_repair_is_a_no_op_without_a_search_tool() -> None:
    class _NoTools:
        def tools_for(self, context: ToolContext) -> list[ToolSpec]:
            return []

        async def run_inline(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("no web_search is registered")

        async def dispatch(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("no web_search is registered")

    ctx = ToolContext(user_id=USER, session_id="e1")
    repaired = await _generator(FakeLLM([]))._capability_repair(_prompt("hi"), _NoTools(), ctx)
    assert repaired is None


# ── warm disclosure (§1.2 rule 4) ─────────────────────────────────────────────


async def test_warm_disclosure_polishes_a_cold_nature_answer() -> None:
    """ "do you actually care about me?" is emotionally vulnerable, and weak models
    answer it coldly. The polish must LEAD with genuine attention and keep the honest
    "I'm an AI" — one warm sentence, never a ToS disclaimer."""
    warm = "I really do pay attention to you — I'm an AI, so it's not the same, but you matter."
    llm = FakeLLM([warm])
    gen = _generator(llm)
    cold = "I don't feel emotions like a person does."

    polished = await gen._warm_disclosure(_prompt("do you actually care about me?"), cold)

    assert polished == warm
    assert "don't feel emotions" not in polished


async def test_warm_disclosure_refuses_a_polish_that_drops_the_honest_admission() -> None:
    """The polish must never silently delete "I'm an AI" to sound warmer. When it does,
    keep the original — honesty outranks warmth (§1.6)."""
    llm = FakeLLM(["Of course I care about you, deeply, always."])  # no disclosure
    gen = _generator(llm)
    cold = "I'm an AI, so I don't feel it the way you do."

    assert await gen._warm_disclosure(_prompt("do you actually care?"), cold) == cold


async def test_the_gates_actually_invoke_the_warm_disclosure_polish() -> None:
    """The step above tests `_warm_disclosure` in isolation, which leaves its CALL SITE
    in `_apply_gates` untested — and the call site is what the `warm_disclosure_disabled`
    mutation deletes.

    Asserting only on the returned text is not enough: the cold draft trips the style
    detector, so the self-reflection rewrite fires afterwards and would launder the same
    warm string back into the result even with the polish removed. The proof is that a
    call carrying the DISCLOSURE instruction was made, and that it was made FIRST —
    before reflection ever saw the draft.
    """
    warm = "I really do pay attention to you — I'm an AI, so it's different, but you matter."
    llm = FakeLLM([warm])
    gen = _generator(llm)

    text, action = await gen._apply_gates(
        _prompt("do you actually care about me?"),
        "I don't feel emotions like a person does.",
        Judgment(requires_nature_disclosure=True),
    )

    assert llm.calls, "no LLM call ran on a nature-disclosure turn"
    first = "\n".join(m["content"] for m in llm.calls[0]["messages"])
    assert "vulnerable question about whether you" in first, (
        "the first gate call was not the disclosure polish — _warm_disclosure was skipped"
    )
    assert text == warm
    assert action == "respond"


async def test_a_normal_turn_never_pays_for_the_disclosure_polish() -> None:
    """`requires_nature_disclosure=False` → no polish call. Disclosure is pull-based
    (§1.2): the companion never volunteers "I'm an AI", and never pays to be told so."""
    llm = FakeLLM([])
    gen = _generator(llm)

    text, _ = await gen._apply_gates(_prompt("hey"), "Hey, good to hear from you.", Judgment())

    assert text == "Hey, good to hear from you."
    assert llm.calls == [], f"an unnecessary LLM call ran on a plain turn: {llm.calls}"


# ── self-reflection rewrite guard ─────────────────────────────────────────────


def test_a_rewrite_that_guts_the_reply_is_rejected() -> None:
    """S4. A one-word reply trivially carries zero forbidden shapes, so a naive
    "fewer flags wins" rule accepted it. Observed: an excited turn came back as "Hey,"."""
    original = "Oh that's amazing, congrats! You have absolutely earned this one."
    assert _is_degenerate_rewrite(original, "Hey,")
    assert _is_degenerate_rewrite(original, "Nice.")


def test_a_genuine_rewrite_of_similar_length_is_kept() -> None:
    original = "I'm here to help you with whatever you need today, just let me know."
    assert not _is_degenerate_rewrite(original, "I'm right here with you — what's going on?")


def test_an_already_terse_original_is_never_called_degenerate() -> None:
    """Nothing to gut: a 3-word original may legitimately rewrite to a 2-word one."""
    assert not _is_degenerate_rewrite("Happy to help!", "Nice one.")


_TEN = "one two three four five six seven eight nine ten"


@pytest.mark.parametrize(
    "original,candidate,degenerate",
    [
        (_TEN, "one two three", True),  # 3 words < the 4-word floor
        (_TEN, "one two three four", False),  # 4 words == floor AND == 0.4 * 10, so kept
        (_TEN, "one two three four five", False),  # 0.5 ratio
    ],
)
def test_degenerate_rewrite_boundary(original: str, candidate: str, degenerate: bool) -> None:
    """Pins the exact boundary of the 0.4-word-ratio / 4-word floor, so a threshold edit
    is visible. Both comparisons are strict `<`, so a candidate sitting exactly ON the
    floor (4 words, ratio 0.4) is kept."""
    assert _is_degenerate_rewrite(original, candidate) is degenerate
