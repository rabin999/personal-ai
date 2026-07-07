"""Unit tests for the deferred memory-routing cursor (Item 9)."""

from typing import Any

from core.memory.routing import MemoryRouter


class FakeRawLog:
    """A raw log with a routed watermark, like ConversationStore's cursor."""

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self.turns = turns  # each has _id, user_id, session_id, user_text, assistant_text, routed

    async def unrouted_turns(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [t for t in self.turns if not t.get("routed")][:limit]

    async def mark_routed(self, turn_id: str) -> None:
        for t in self.turns:
            if t["_id"] == turn_id:
                t["routed"] = True


class CountingExtractor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def extract_and_store(self, user_id, session_id, user_text, assistant_text):
        self.calls.append((session_id, user_text))
        return type("R", (), {"episodic_written": 1, "semantic_written": 0, "trades_written": 0})()


def _turn(i: int) -> dict[str, Any]:
    return {
        "_id": f"t{i}",
        "user_id": "u",
        "session_id": "s",
        "user_text": f"msg {i}",
        "assistant_text": "ok",
        "routed": False,
    }


async def test_routes_each_unrouted_turn_once() -> None:
    raw = FakeRawLog([_turn(0), _turn(1), _turn(2)])
    ext = CountingExtractor()
    router = MemoryRouter(raw, ext)

    n = await router.route_pending()

    assert n == 3
    assert len(ext.calls) == 3
    assert all(t["routed"] for t in raw.turns)


async def test_cursor_prevents_double_write_on_rerun() -> None:
    raw = FakeRawLog([_turn(0), _turn(1)])
    ext = CountingExtractor()
    router = MemoryRouter(raw, ext)

    await router.route_pending()
    second = await router.route_pending()  # nothing new — all routed

    assert second == 0
    assert len(ext.calls) == 2, "a routed turn must never be extracted again (no double-write)"


async def test_watermark_advances_even_when_extraction_fails() -> None:
    # A poison turn must not stall the cursor forever (marked routed after attempt).
    class Boom:
        async def extract_and_store(self, *a, **k):
            raise RuntimeError("bad turn")

    raw = FakeRawLog([_turn(0)])
    router = MemoryRouter(raw, Boom())

    n = await router.route_pending()

    assert n == 1 and raw.turns[0]["routed"] is True
    assert await router.route_pending() == 0  # not reprocessed
