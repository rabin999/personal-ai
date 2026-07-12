"""Unit tests for the slow-turn progress fillers (§8.12): while a search/generation runs,
keep the user in the loop with a short honest progress line each time the audio goes quiet
past the gap — without ever talking over the answer as it streams.

Exercises `ResponseGenerator._speak_with_fillers` in isolation with a scripted "work" coroutine
and a tiny gap, so the timing behaviour is deterministic and fast."""

import asyncio

from core.profile import ProfileService, TraitRegistry
from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import (
    _ACK_PROGRESS_APOLOGY,
    _ACK_PROGRESS_LOOKUP,
    _ACK_PROGRESS_THINKING,
    ResponseGenerator,
)
from core.reasoning.self_model import SelfModel
from tests.fakes import FakeDocStore, FakeLLM, FakeVectorStore

USER = "u_demo_001"
_PROGRESS = set(_ACK_PROGRESS_LOOKUP) | set(_ACK_PROGRESS_THINKING) | set(_ACK_PROGRESS_APOLOGY)


def _prompt() -> AssembledPrompt:
    return AssembledPrompt(
        user_id=USER,
        session_id="s1",
        utterance="who is the prime minister of nepal right now?",
        system_prompt="You are Companion.",
        messages=[{"role": "user", "content": "who is the pm of nepal?"}],
        complexity_hint="simple",
    )


def _gen(gap: float = 0.02, max_fillers: int = 3, apology_after: int = 99) -> ResponseGenerator:
    # apology_after defaults high so tests that assert on the BRIEF pools stay brief; the
    # escalation test lowers it explicitly.
    docs = FakeDocStore()
    vectors = FakeVectorStore()
    llm = FakeLLM([])
    registry = TraitRegistry(docs, ProfileService(docs))
    self_model = SelfModel(docs, vectors, llm)
    return ResponseGenerator(
        llm,
        self_model,
        registry,
        progress_filler_gap_s=gap,
        progress_filler_max=max_fillers,
        progress_filler_apology_after=apology_after,
    )


class _Recorder:
    """Captures spoken text and flush() calls, standing in for the voice TTS closures."""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.flushes = 0

    async def speak(self, text: str) -> None:
        if text.strip():
            self.spoken.append(text.strip())

    async def flush(self) -> None:
        self.flushes += 1

    @property
    def fillers(self) -> list[str]:
        return [s for s in self.spoken if s in _PROGRESS]


async def test_silent_slow_work_emits_progress_fillers_up_to_cap() -> None:
    """A slow turn that never speaks (search running in the dead air) should get progress
    lines each gap, capped at progress_filler_max."""
    gen = _gen(gap=0.02, max_fillers=3)
    rec = _Recorder()

    async def work(_speak: object) -> str:
        await asyncio.sleep(0.3)  # >> gap*max → the cap, not the clock, ends the fillers
        return "answer"

    result = await gen._speak_with_fillers(_prompt(), rec.speak, rec.flush, work, is_lookup=True)
    assert result == "answer"
    assert len(rec.fillers) == 3  # exactly the cap
    assert rec.flushes == 3  # each filler flushed as its own utterance
    assert set(rec.fillers) <= set(_ACK_PROGRESS_LOOKUP)  # lookup pool for a live query


async def test_fast_work_emits_no_fillers() -> None:
    """A turn that answers within the gap must not get a progress line."""
    gen = _gen(gap=0.5, max_fillers=3)
    rec = _Recorder()

    async def work(_speak: object) -> str:
        return "quick"

    result = await gen._speak_with_fillers(_prompt(), rec.speak, rec.flush, work, is_lookup=True)
    assert result == "quick"
    assert rec.fillers == []
    assert rec.flushes == 0


async def test_streaming_answer_resets_the_clock() -> None:
    """When the work streams real chunks faster than the gap, the silence clock keeps
    resetting, so no filler fires — the filler never talks over the answer."""
    gen = _gen(gap=0.05, max_fillers=5)
    rec = _Recorder()

    async def work(speak: "object") -> str:
        for i in range(6):
            await asyncio.sleep(0.02)  # < gap
            await speak(f"chunk {i}.")  # type: ignore[operator]
        return "done"

    result = await gen._speak_with_fillers(_prompt(), rec.speak, rec.flush, work, is_lookup=True)
    assert result == "done"
    assert rec.fillers == []  # every chunk reset the clock before the gap elapsed
    assert any(s.startswith("chunk") for s in rec.spoken)


async def test_gap_between_chunks_gets_one_filler() -> None:
    """A real stall mid-answer (a chunk, then a long silence, then more) should be filled
    once — the watchdog re-arms after each real chunk."""
    gen = _gen(gap=0.03, max_fillers=5)
    rec = _Recorder()

    async def work(speak: "object") -> str:
        await speak("here's the first part.")  # type: ignore[operator]
        await asyncio.sleep(0.12)  # a genuine gap → one (or more) filler
        await speak("and the rest.")  # type: ignore[operator]
        return "done"

    await gen._speak_with_fillers(_prompt(), rec.speak, rec.flush, work, is_lookup=True)
    assert len(rec.fillers) >= 1
    # The real answer chunks are still spoken, in order, around the filler.
    assert rec.spoken[0] == "here's the first part."
    assert rec.spoken[-1] == "and the rest."


async def test_thinking_pool_for_non_lookup() -> None:
    """A non-lookup slow turn draws from the 'thinking' pool, not the 'searching' pool."""
    gen = _gen(gap=0.02, max_fillers=2)
    rec = _Recorder()

    async def work(_speak: object) -> str:
        await asyncio.sleep(0.2)
        return "answer"

    await gen._speak_with_fillers(_prompt(), rec.speak, rec.flush, work, is_lookup=False)
    assert rec.fillers
    assert set(rec.fillers) <= set(_ACK_PROGRESS_THINKING)


async def test_disabled_by_config_emits_nothing() -> None:
    """progress_filler_max=0 turns the feature off entirely, but the work still runs."""
    gen = _gen(gap=0.02, max_fillers=0)
    rec = _Recorder()

    async def work(_speak: object) -> str:
        await asyncio.sleep(0.2)
        return "answer"

    result = await gen._speak_with_fillers(_prompt(), rec.speak, rec.flush, work, is_lookup=True)
    assert result == "answer"
    assert rec.spoken == []


async def test_work_exception_still_cancels_watchdog() -> None:
    """If the work raises, the failure propagates and the watchdog is torn down (no hang)."""
    gen = _gen(gap=0.02, max_fillers=3)
    rec = _Recorder()

    async def work(_speak: object) -> str:
        await asyncio.sleep(0.05)
        raise RuntimeError("boom")

    try:
        await asyncio.wait_for(
            gen._speak_with_fillers(_prompt(), rec.speak, rec.flush, work, is_lookup=True),
            timeout=2.0,
        )
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert str(exc) == "boom"


async def test_tone_softens_to_apology_after_threshold() -> None:
    """As the wait drags, the first couple of nudges stay brisk; after apology_after of them
    the tone softens to a gentle apology ('so sorry it's taking longer than expected')."""
    gen = _gen(gap=0.02, max_fillers=5, apology_after=2)
    rec = _Recorder()

    async def work(_speak: object) -> str:
        await asyncio.sleep(0.4)  # long enough to hit the cap of 5
        return "answer"

    await gen._speak_with_fillers(_prompt(), rec.speak, rec.flush, work, is_lookup=True)
    assert len(rec.fillers) == 5
    # First two: brisk lookup nudges. Remaining three: gentle apologies.
    assert all(f in set(_ACK_PROGRESS_LOOKUP) for f in rec.fillers[:2])
    assert all(f in set(_ACK_PROGRESS_APOLOGY) for f in rec.fillers[2:])
