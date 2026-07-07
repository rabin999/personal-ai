"""Unit tests for per-turn trace totals roll-up (Item 6 / §3.12)."""

from api.routes.debug import _turn_totals


def _span(turn, stage, **data):
    return {"turn": turn, "stage": stage, "message": "", "data": data}


def test_totals_sum_tokens_cost_and_count_steps() -> None:
    events = [
        _span(1, "session", text="hi"),
        _span(1, "llm", input_tokens=100, output_tokens=20, cost_usd=0.001, model="x"),
        _span(1, "llm", tokens_in=50, tokens_out=10, usd=0.0005),  # unified names
        _span(1, "tool", status="success", tool="web_search"),
        _span(1, "tool", status="failure", tool="broken"),
        _span(1, "reflection", ran=True),
        _span(1, "session", total_ms=1234.5),
    ]
    (t,) = _turn_totals(events)
    assert t["turn"] == 1
    assert t["tokens_in"] == 150 and t["tokens_out"] == 30
    assert t["cost_usd"] == 0.0015
    assert t["llm_calls"] == 2
    assert t["tool_calls"] == 2 and t["failures"] == 1
    assert t["reflected"] is True
    assert t["total_ms"] == 1234.5


def test_totals_group_by_turn_in_order() -> None:
    events = [
        _span(2, "llm", cost_usd=0.002),
        _span(1, "llm", cost_usd=0.001),
    ]
    totals = _turn_totals(events)
    assert [t["turn"] for t in totals] == [1, 2]


def test_totals_tolerate_missing_or_bad_numbers() -> None:
    events = [_span(1, "llm", cost_usd="not-a-number", input_tokens=None)]
    (t,) = _turn_totals(events)
    assert t["cost_usd"] == 0.0 and t["tokens_in"] == 0
