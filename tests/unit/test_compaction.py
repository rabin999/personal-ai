"""Unit tests for long-session compaction (F14): the working-memory buffer is
bounded by folding older turns into a rolling summary, deterministically."""

import pytest

from core.memory.compaction import COMPACT_THRESHOLD, KEEP_RECENT, SessionCompactor
from core.memory.working import Turn, WorkingMemory
from ports.llm import CompletionResult


class FakeLLM:
    """Returns a canned summary; records that it was asked to summarize."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_prompt = ""

    async def complete(self, user_id, messages, tier="moderate", **kwargs) -> CompletionResult:
        self.calls += 1
        self.last_prompt = messages[0]["content"]
        return CompletionResult(
            text="The user's daughter Ishani turns 5 next month. They discussed a trip.",
            model="fake",
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.0,
        )


def _fill(wm: WorkingMemory, session: str, n: int) -> None:
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        wm.append(session, Turn(role=role, text=f"turn {i}"))


def test_compact_trims_buffer_and_keeps_summary() -> None:
    wm = WorkingMemory()
    s = "s1"
    wm.append(s, Turn(role="user", text="my daughter Ishani turns 5 next month"))
    _fill(wm, s, 4)
    summary = wm.compact(s, keep_recent=2, summary="Ishani turns 5; planning a trip.")
    assert summary == 3  # 5 turns → keep 2 → dropped 3
    assert wm.size(s) == 2
    assert "Ishani" in wm.summary(s)
    # The dropped turns are gone from the live buffer (they live in the store).
    assert [t.text for t in wm.recent(s, 5)] == ["turn 2", "turn 3"]


def test_compact_noop_when_small() -> None:
    wm = WorkingMemory()
    _fill(wm, "s", 3)
    assert wm.compact("s", keep_recent=8, summary="x") == 0
    assert wm.size("s") == 3  # nothing dropped


@pytest.mark.asyncio
async def test_compactor_bounds_a_long_session() -> None:
    wm = WorkingMemory()
    llm = FakeLLM()
    comp = SessionCompactor(llm, wm)  # type: ignore[arg-type]
    s = "long"
    wm.append(s, Turn(role="user", text="my daughter Ishani turns 5 next month"))
    _fill(wm, s, COMPACT_THRESHOLD + 5)  # well over threshold

    assert comp.should_compact(s)
    dropped = await comp.maybe_compact(s, "u")
    assert dropped > 0
    assert llm.calls == 1  # summarized the overflow
    # The buffer is now bounded to KEEP_RECENT, no matter how long the session ran.
    assert wm.size(s) == KEEP_RECENT
    # The early fact survives in the rolling summary (recall stays possible).
    assert "Ishani" in wm.summary(s)
    # The overflow turns were handed to the summarizer.
    assert "Ishani" in llm.last_prompt


@pytest.mark.asyncio
async def test_compaction_keeps_prompt_bounded_across_growth() -> None:
    """The whole point: the live buffer size stops growing once compaction kicks in,
    so the prompt (built from recent turns + a fixed-size summary) stays bounded."""
    wm = WorkingMemory()
    comp = SessionCompactor(FakeLLM(), wm)  # type: ignore[arg-type]
    s = "grow"
    sizes = []
    for i in range(COMPACT_THRESHOLD * 3):
        wm.append(s, Turn(role="user" if i % 2 == 0 else "assistant", text=f"msg {i}"))
        if comp.should_compact(s):
            await comp.maybe_compact(s, "u")
        sizes.append(wm.size(s))
    # Despite 3x-threshold turns appended, the buffer never exceeds the threshold.
    assert max(sizes) <= COMPACT_THRESHOLD + 1
