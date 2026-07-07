"""Unit tests for the response feedback store + episodic list/delete (Part C)."""

from core.feedback import FeedbackStore
from core.memory.episodic import EpisodicMemory
from tests.fakes import FakeDocStore, FakeVectorStore

A = "u_demo_001"
B = "u_demo_002"


async def test_feedback_records_and_lists_user_scoped_newest_first() -> None:
    store = FeedbackStore(FakeDocStore())
    await store.record(user_id=A, session_id="s1", rating="up", turn_id="1")
    await store.record(user_id=A, session_id="s1", rating="down", note="wrong", turn_id="2")
    await store.record(user_id=B, session_id="s9", rating="up")

    items, total = await store.list_for_user(A)
    assert total == 2  # §0.5: B's feedback excluded
    assert items[0]["rating"] == "down" and items[0]["turn_id"] == "2"  # newest first
    assert items[0]["trace_id"] == "s1"  # ties back to the trace


async def test_feedback_filter_by_rating() -> None:
    store = FeedbackStore(FakeDocStore())
    await store.record(user_id=A, session_id="s1", rating="up")
    await store.record(user_id=A, session_id="s1", rating="down")
    downs, total = await store.list_for_user(A, rating="down")
    assert total == 1 and downs[0]["rating"] == "down"


async def test_episodic_list_and_delete_are_user_scoped() -> None:
    vectors = FakeVectorStore()
    epi = EpisodicMemory(vectors)
    await epi.write(A, "s1", ["A took meds at 8pm"])
    await epi.write(B, "s2", ["B secret"])

    a_items = await epi.list_recent(A)
    assert len(a_items) == 1 and "meds" in a_items[0].text
    mem_id = a_items[0].id
    assert mem_id is not None

    # B cannot delete A's memory; A can.
    assert await epi.delete(B, mem_id) is False
    assert await epi.delete(A, mem_id) is True
    assert await epi.list_recent(A) == []
