"""When a reply's ONLY flaw is a stock filler QUESTION ('what's on your mind?'), the
self-reflection step must REPLACE it with a genuine/reciprocal question — not delete the whole
sentence and leave a dead-end. The reported bug: "I'm great, how are you?" → the model drafted
"Doing well, thanks for asking! Sounds like you're in a good place — what's on your mind?", the
scrubber deleted the engaging tail as a "flat filler opener", and the user heard the flat
"Doing well, thanks for asking!" with nothing to respond to.

Verifies the routing (flat-filler-only flags → the engagement rewrite, not the delete-scrub)
with a scripted LLM so it's deterministic; the wording quality is proven by the real_call probe.
"""

from core.profile import ProfileService, TraitRegistry
from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import _ENGAGEMENT_FILLER_LABELS, ResponseGenerator
from core.reasoning.self_model import SelfModel
from core.reasoning.style import find_forbidden
from tests.fakes import FakeDocStore, FakeLLM, FakeVectorStore

USER = "u_demo_001"


def _prompt() -> AssembledPrompt:
    return AssembledPrompt(
        user_id=USER,
        session_id="s1",
        utterance="I'm great, how are you?",
        system_prompt="You are Companion.",
        messages=[{"role": "user", "content": "I'm great, how are you?"}],
        complexity_hint="simple",
    )


def test_flat_filler_labels_are_what_the_detector_emits() -> None:
    # The routing hinges on these exact label strings — guard them against a rename.
    flags = find_forbidden("Doing well! So what's on your mind?")
    assert set(flags) <= _ENGAGEMENT_FILLER_LABELS
    assert flags, "the stock question must be detected in the first place"


async def test_repair_replaces_stock_question_with_genuine_one() -> None:
    docs = FakeDocStore()
    # The engagement rewrite's LLM call returns a genuine reciprocal question (no stock filler).
    llm = FakeLLM(["Doing good, thanks — how's YOUR day been treating you?"])
    gen = ResponseGenerator(
        llm, SelfModel(docs, FakeVectorStore(), llm), TraitRegistry(docs, ProfileService(docs))
    )

    out = await gen._repair_flat_filler(
        _prompt(),
        "Doing well, thanks for asking! Sounds like you're in a good place — what's on your mind?",
        allow_disclosure=False,
    )
    assert "on your mind" not in out.lower(), "the stock filler must be gone"
    assert "?" in out, "engagement is preserved — it still asks them something"
    assert not find_forbidden(out), "the result is clean of forbidden shapes"


async def test_repair_falls_back_to_clean_scrub_if_rewrite_still_dirty() -> None:
    docs = FakeDocStore()
    # The rewrite fails to clear the stock phrase → we must fall back to a clean (if flat) reply,
    # never ship the forbidden phrase.
    llm = FakeLLM(["Sure — what's on your mind then?"])
    gen = ResponseGenerator(
        llm, SelfModel(docs, FakeVectorStore(), llm), TraitRegistry(docs, ProfileService(docs))
    )

    out = await gen._repair_flat_filler(
        _prompt(),
        "Doing well! Anyway, what's on your mind?",
        allow_disclosure=False,
    )
    assert not find_forbidden(out), (
        "never ship the stock filler, even when the rewrite is unhelpful"
    )
    assert out.strip(), "and never empty the reply to nothing"
