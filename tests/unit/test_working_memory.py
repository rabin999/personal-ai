"""Unit tests for Working Memory (spec §4).

This module is pure in-process state with no external dependencies, so unit
tests are the complete verification; its close-output handoff is exercised
end-to-end by the Episodic Memory tests (§5).
"""

from core.memory.working import Turn, WorkingMemory


def _turn(text: str, role: str = "user") -> Turn:
    return Turn.model_validate({"role": role, "text": text})


def test_recent_returns_exactly_the_last_n_turns_in_order() -> None:
    wm = WorkingMemory()
    for i in range(5):
        wm.append("s1", _turn(f"turn-{i}"))

    recent = wm.recent("s1", n=3)

    assert [t.text for t in recent] == ["turn-2", "turn-3", "turn-4"]


def test_close_returns_full_transcript_and_clears_buffer() -> None:
    wm = WorkingMemory()
    wm.append("s1", _turn("hello"))
    wm.append("s1", _turn("hi there", role="assistant"))

    transcript = wm.close("s1")

    assert [t.text for t in transcript] == ["hello", "hi there"]
    assert wm.recent("s1") == []
    assert wm.all("s1") == []


def test_new_session_starts_empty_and_buffers_are_isolated() -> None:
    wm = WorkingMemory()
    wm.append("s1", _turn("session one"))

    assert wm.recent("s2") == []
    wm.append("s2", _turn("session two"))
    assert [t.text for t in wm.all("s1")] == ["session one"]
    assert [t.text for t in wm.all("s2")] == ["session two"]


def test_default_recent_window_is_eight() -> None:
    wm = WorkingMemory()
    for i in range(12):
        wm.append("s1", _turn(f"t{i}"))
    assert len(wm.recent("s1")) == 8


def test_turn_gets_timestamp_and_optional_fields_default_none() -> None:
    turn = _turn("hello")
    assert turn.timestamp
    assert turn.emotion is None and turn.meta is None


def test_close_unknown_session_returns_empty() -> None:
    assert WorkingMemory().close("never-existed") == []
