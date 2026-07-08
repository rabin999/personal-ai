"""Real-store background-delivery carry (brief U9) — real Redis queue.

Proves a completed result is retrievable across sessions (carried to the next
conversation open) and that the current session is excluded, user-scoped.
"""

import uuid

import pytest

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]


async def test_completed_result_carries_across_sessions(real_turns) -> None:
    q = real_turns._p.queue
    user = f"u_carry_{uuid.uuid4().hex[:8]}"
    old_session = "s_old"
    # A task started in a prior session, then completed.
    task_id = await q.enqueue(
        session_id=old_session, user_id=user, type="web_search", params={"query": "look up a book"}
    )
    await q.complete(task_id, {"summary": "found the book"})

    # At the NEXT conversation open (a new session), the result is carried over.
    carried = await q.pending_deliveries_for_user(user, exclude_session="s_new")
    assert [t.task_id for t in carried] == [task_id]

    # Excluding the OLD session (as the in-session path does) yields nothing to carry.
    assert await q.pending_deliveries_for_user(user, exclude_session=old_session) == []

    # Isolation: another user never sees it.
    assert await q.pending_deliveries_for_user(f"u_x_{uuid.uuid4().hex[:6]}") == []

    # Once delivered, it's no longer pending.
    await q.mark_delivered(task_id)
    assert await q.pending_deliveries_for_user(user, exclude_session="s_new") == []
