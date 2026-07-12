"""A companion-INITIATED turn (open greeting / silence-lull check-in) must never run the
wait-fillers. The reported failure: a lull check-in — a single "you still there?" line — first
spoke an `ack_recall` interjection, then three progress lines, then a "sorry this is dragging
on" apology, because the check-in went through the same `generate_spoken` machinery a user
question does, and its bracketed directive tripped the recall/quick-ack + progress-filler path.

`proactive=True` suppresses BOTH the quick interjection (`_dynamic_ack`) and the progress-filler
loop (`_speak_with_fillers`): a proactive one-liner has no user question being kept waiting, so
there is nothing to fill. These tests drive `generate_spoken` with the filler-triggering paths
stubbed and assert which of them actually fire under each flag.
"""

from typing import Any, cast

import pytest

from core.profile import ProfileService, TraitRegistry
from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import GenerationResult, ResponseGenerator
from core.reasoning.self_model import SelfModel
from tests.fakes import FakeDocStore, FakeLLM, FakeVectorStore

USER = "u_demo_001"


def _gen() -> ResponseGenerator:
    docs = FakeDocStore()
    llm = FakeLLM([])
    registry = TraitRegistry(docs, ProfileService(docs))
    self_model = SelfModel(docs, FakeVectorStore(), llm)
    return ResponseGenerator(llm, self_model, registry, progress_filler_gap_s=0.01)


def _recall_prompt() -> AssembledPrompt:
    # recall_source="past" makes `_wants_quick_ack` True and keeps the turn streamable — exactly
    # the shape the lull check-in's directive took when it misfired the fillers.
    return AssembledPrompt(
        user_id=USER,
        session_id="s1",
        utterance="you still there?",
        system_prompt="You are Companion.",
        messages=[{"role": "user", "content": "you still there?"}],
        complexity_hint="simple",
        recall_source="past",
    )


class _Spy:
    def __init__(self, gen: ResponseGenerator) -> None:
        self.acks = 0
        self.filler_runs = 0
        self.spoken: list[str] = []

        async def _ack(*_a: object, **_k: object) -> None:
            self.acks += 1

        async def _stream_reply(*_a: object, **_k: object) -> None:
            return None  # force the fall-through to the (normally filler-wrapped) slow branch

        async def _generate(*_a: object, **_k: object) -> GenerationResult:
            return GenerationResult(
                final_text="Hey, you there?", voice_text="Hey, you there?", action="respond"
            )

        async def _fillers(_p: object, _s: object, _f: object, make_work: object, **_k: object):
            self.filler_runs += 1
            return await make_work(self.speak)  # type: ignore[operator]

        gen._dynamic_ack = _ack  # type: ignore[method-assign]
        gen._stream_reply = _stream_reply  # type: ignore[method-assign]
        gen.generate = _generate  # type: ignore[method-assign]
        gen._speak_with_fillers = _fillers  # type: ignore[method-assign,assignment]

    async def speak(self, text: str) -> None:
        if text.strip():
            self.spoken.append(text.strip())


async def test_proactive_turn_runs_no_fillers() -> None:
    gen = _gen()
    spy = _Spy(gen)
    result = await gen.generate_spoken(
        _recall_prompt(), cast(Any, object()), cast(Any, object()), spy.speak, proactive=True
    )
    assert result.voice_text == "Hey, you there?"
    assert spy.acks == 0, "a proactive turn must not speak a quick interjection"
    assert spy.filler_runs == 0, "a proactive turn must not run the progress-filler loop"
    assert spy.spoken == ["Hey, you there?"], "only the actual line is spoken — no filler cascade"


async def test_non_proactive_same_prompt_does_run_fillers() -> None:
    """Control: the identical prompt WITHOUT the proactive flag still takes the filler path, so
    the bypass above is doing the work — not the stubs."""
    gen = _gen()
    spy = _Spy(gen)
    await gen.generate_spoken(
        _recall_prompt(), cast(Any, object()), cast(Any, object()), spy.speak, proactive=False
    )
    assert spy.acks == 1, "a normal recall turn fires the quick interjection"
    assert spy.filler_runs == 1, "a normal slow turn runs the progress-filler loop"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
