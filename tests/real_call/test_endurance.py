"""Real-call long-session endurance (F14): over a long session the prompt stays
bounded AND early context is still recalled — via the rolling summary — with real
model summarization + a real recall turn.

Efficient: the long buffer is built directly (cheap), then ONE real summarization +
ONE real recall turn prove the property, instead of hundreds of paid turns.
"""

import uuid

import pytest

from core.memory.compaction import COMPACT_THRESHOLD, KEEP_RECENT
from core.memory.working import Turn

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]


def _system_prompt(prompt) -> str:
    return prompt.messages[0]["content"] if prompt.messages else ""


async def test_long_session_stays_bounded_and_recalls_early_context(real_turns) -> None:
    wm = real_turns.working
    s = f"end_{uuid.uuid4().hex[:6]}"
    # A UNIQUE marker per run (kept separate from the name so the model can't
    # reformat it) so a prior run's persisted recall turn can't contaminate the
    # shared demo user's memory and pre-seed the "before" assertion.
    marker = uuid.uuid4().hex[:6]
    early_fact = (
        f"my daughter Ishani turns 5 next month and loves dinosaurs; "
        f"our family code word is {marker}"
    )
    q = "what's my daughter's name?"
    # Turn 0 carries the distinctive early fact; then a long run of filler turns —
    # far more than the 8-turn recent window the prompt normally shows.
    wm.append(s, Turn(role="user", text=early_fact))
    for i in range(COMPACT_THRESHOLD + 6):
        wm.append(
            s, Turn(role="user" if i % 2 == 0 else "assistant", text=f"just chatting, item {i}")
        )

    # BEFORE compaction: the early fact is beyond the recent window and there's no
    # summary yet, so it is NOT in the assembled prompt (a long session would forget it).
    before = await real_turns.assembler.assemble(real_turns.user_id, s, q)
    assert marker not in _system_prompt(before), "early fact unexpectedly already in prompt"
    size_before = len(_system_prompt(before))

    # Compact: real model folds the overflow into the rolling summary; buffer bounded.
    dropped = await real_turns.compactor.maybe_compact(s, real_turns.user_id)
    assert dropped > 0, "compaction did not run"
    assert wm.size(s) == KEEP_RECENT, f"buffer not bounded after compaction: {wm.size(s)}"
    summary = wm.summary(s).lower()
    assert marker in summary and "ishani" in summary, (
        f"early fact lost from summary: {wm.summary(s)}"
    )

    # AFTER compaction: the early fact survives IN the prompt via the running summary,
    # and the prompt stayed bounded (not proportional to the ~30-turn history).
    after = await real_turns.assembler.assemble(real_turns.user_id, s, q)
    assert "ishani" in _system_prompt(after).lower(), "early fact not carried by the summary"
    size_after = len(_system_prompt(after))
    # Bounded: the compacted prompt is not dramatically larger than the pre-summary one
    # (the summary is a few paragraphs, not the whole transcript).
    assert size_after < size_before + 4000, f"prompt grew unbounded: {size_before}→{size_after}"

    # A real recall turn late in the session still answers from the summarized thread.
    reply = (await real_turns.say(q, s)).reply
    assert "ishani" in reply.lower(), f"failed to recall early context late in session: {reply}"
