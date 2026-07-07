"""Unit tests for prompt-version performance attribution (Item 7 / §7)."""

from core.observability.attribution import (
    attribute_by_prompt_version,
    prompt_version_by_turn,
)


def _assembly(session, turn, version):
    return {
        "session_id": session,
        "turn": turn,
        "stage": "assembly",
        "data": {"prompt_version": version},
    }


def _fb(session, turn, rating):
    return {"session_id": session, "turn_id": str(turn), "rating": rating}


def test_prompt_version_by_turn_reads_assembly_spans() -> None:
    events = [
        _assembly("s1", 1, "pt2.aaaa"),
        {"session_id": "s1", "turn": 1, "stage": "llm", "data": {}},  # ignored
        _assembly("s1", 2, "pt2.bbbb"),
    ]
    m = prompt_version_by_turn(events)
    assert m[("s1", 1)] == "pt2.aaaa"
    assert m[("s1", 2)] == "pt2.bbbb"


def test_attribution_groups_and_ranks_by_up_rate() -> None:
    # Version A: 3 up / 1 down (0.75). Version B: 1 up / 3 down (0.25).
    vbt = {
        ("s", 1): "pt2.A",
        ("s", 2): "pt2.A",
        ("s", 3): "pt2.A",
        ("s", 4): "pt2.A",
        ("s", 5): "pt2.B",
        ("s", 6): "pt2.B",
        ("s", 7): "pt2.B",
        ("s", 8): "pt2.B",
    }
    feedback = [
        _fb("s", 1, "up"),
        _fb("s", 2, "up"),
        _fb("s", 3, "up"),
        _fb("s", 4, "down"),
        _fb("s", 5, "up"),
        _fb("s", 6, "down"),
        _fb("s", 7, "down"),
        _fb("s", 8, "down"),
    ]
    rows = attribute_by_prompt_version(feedback, vbt)
    by = {r["prompt_version"]: r for r in rows}
    assert by["pt2.A"]["up_rate"] == 0.75 and by["pt2.A"]["n"] == 4
    assert by["pt2.B"]["up_rate"] == 0.25 and by["pt2.B"]["n"] == 4
    # Best version ranks first — the whole point of the attribution view.
    assert rows[0]["prompt_version"] == "pt2.A"


def test_unmatched_feedback_bucketed_as_unknown_not_dropped() -> None:
    rows = attribute_by_prompt_version([_fb("s", 99, "up")], {})
    assert rows and rows[-1]["prompt_version"] == "unknown"
    assert rows[-1]["thumbs_up"] == 1


def test_judge_scores_averaged_per_version() -> None:
    vbt = {("s", 1): "pt2.A", ("s", 2): "pt2.A"}
    rows = attribute_by_prompt_version(
        [_fb("s", 1, "up"), _fb("s", 2, "up")],
        vbt,
        judge_scores={("s", 1): 5.0, ("s", 2): 3.0},
    )
    assert rows[0]["avg_judge_score"] == 4.0
