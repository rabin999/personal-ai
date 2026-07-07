"""Unit tests for the durable raw conversation store (§6) — faked DocStore."""

from core.memory.conversation_store import (
    CONVERSATION_TURNS_COLLECTION,
    ConversationStore,
)
from tests.fakes import FakeDocStore

USER_A = "u_demo_001"
USER_B = "u_demo_002"


async def _record(store: ConversationStore, user: str, session: str, n: int) -> None:
    for i in range(1, n + 1):
        await store.record_turn(
            user_id=user,
            session_id=session,
            turn_index=i,
            user_text=f"u{i}",
            assistant_text=f"a{i}",
        )


async def test_records_turns_and_session_header() -> None:
    docs = FakeDocStore()
    store = ConversationStore(docs)
    await _record(store, USER_A, "s1", 3)

    turns = await store.turns(USER_A, "s1")
    assert [t["user_text"] for t in turns] == ["u1", "u2", "u3"]  # in order
    convos, total = await store.list_conversations(USER_A)
    assert total == 1
    assert convos[0]["turn_count"] == 3  # header updated per turn


async def test_list_is_user_scoped_and_paginated() -> None:
    store = ConversationStore(FakeDocStore())
    await _record(store, USER_A, "s1", 1)
    await _record(store, USER_A, "s2", 1)
    await _record(store, USER_A, "s3", 1)
    await _record(store, USER_B, "sB", 1)  # other user

    page1, total = await store.list_conversations(USER_A, offset=0, limit=2)
    assert total == 3 and len(page1) == 2  # §0.5: B's convo excluded from the count
    page2, _ = await store.list_conversations(USER_A, offset=2, limit=2)
    assert len(page2) == 1
    ids = {c["session_id"] for c in [*page1, *page2]}
    assert ids == {"s1", "s2", "s3"} and "sB" not in ids


async def test_datetime_range_filter_is_server_side() -> None:
    docs = FakeDocStore()
    store = ConversationStore(docs)
    await _record(store, USER_A, "old", 1)
    await _record(store, USER_A, "new", 1)
    # Force distinct timestamps on the two session headers.
    old = await docs.get("conversations", "old")
    new = await docs.get("conversations", "new")
    assert old is not None and new is not None
    old["last_ts"] = 100.0
    await docs.put("conversations", "old", old)
    new["last_ts"] = 200.0
    await docs.put("conversations", "new", new)

    only_new, total = await store.list_conversations(USER_A, start_ts=150.0)
    assert total == 1 and only_new[0]["session_id"] == "new"
    only_old, total = await store.list_conversations(USER_A, end_ts=150.0)
    assert total == 1 and only_old[0]["session_id"] == "old"


async def test_turns_are_user_scoped() -> None:
    docs = FakeDocStore()
    store = ConversationStore(docs)
    await _record(store, USER_A, "shared", 2)
    await _record(store, USER_B, "shared", 2)  # same session id, different user

    a = await store.turns(USER_A, "shared")
    assert len(a) == 2 and all(t["user_id"] == USER_A for t in a)
    rows_b = await docs.find(CONVERSATION_TURNS_COLLECTION, {"user_id": USER_B})
    assert len(rows_b) == 2  # B's turns stored separately, never returned to A


async def test_reads_drop_mongo_id_for_json_serialization() -> None:
    # Regression: real Mongo docs carry a non-JSON-serializable ObjectId _id;
    # both reads must strip it so the API can serialize them.
    store = ConversationStore(FakeDocStore())
    await _record(store, USER_A, "s1", 1)
    convos, _ = await store.list_conversations(USER_A)
    turns = await store.turns(USER_A, "s1")
    assert all("_id" not in c for c in convos)
    assert all("_id" not in t for t in turns)
