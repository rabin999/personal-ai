"""Unit tests for the durable trace store (brief §1) — faked DocStore."""

from core.observability import TraceStore
from tests.fakes import FakeDocStore

USER_A = "u_demo_001"
USER_B = "u_demo_002"


def _event(session: str, turn: int, ts: float, stage: str) -> dict[str, object]:
    return {
        "session_id": session,
        "turn": turn,
        "ts": ts,
        "stage": stage,
        "message": f"{stage} event",
        "level": "info",
        "data": {},
    }


async def test_records_and_returns_events_turn_ordered() -> None:
    store = TraceStore(FakeDocStore())
    # Insert out of order; store must return them turn- then ts-ordered.
    await store.record(USER_A, _event("s1", 2, 20.0, "tts"))
    await store.record(USER_A, _event("s1", 1, 10.0, "stt"))
    await store.record(USER_A, _event("s1", 1, 11.0, "generation"))

    events = await store.traces_for(USER_A, "s1")
    assert [e["stage"] for e in events] == ["stt", "generation", "tts"]


async def test_traces_are_user_scoped() -> None:
    # §0.5: user B must never see user A's trace, even for the same session id.
    store = TraceStore(FakeDocStore())
    await store.record(USER_A, _event("shared", 1, 1.0, "stt"))
    await store.record(USER_B, _event("shared", 1, 1.0, "stt"))

    a = await store.traces_for(USER_A, "shared")
    b = await store.traces_for(USER_B, "shared")
    assert len(a) == 1 and len(b) == 1
    assert all(e["user_id"] == USER_A for e in a)
    assert all(e["user_id"] == USER_B for e in b)


async def test_recent_sessions_lists_only_this_users_sessions() -> None:
    store = TraceStore(FakeDocStore())
    await store.record(USER_A, _event("s_old", 1, 1.0, "stt"))
    await store.record(USER_A, _event("s_new", 1, 99.0, "stt"))
    await store.record(USER_B, _event("s_other", 1, 50.0, "stt"))

    total, sessions = await store.recent_sessions(USER_A)
    ids = [s["session_id"] for s in sessions]
    assert total == 2
    assert ids == ["s_new", "s_old"]  # most-recent first
    assert "s_other" not in ids
