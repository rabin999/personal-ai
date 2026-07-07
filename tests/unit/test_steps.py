"""Unit tests for the unified step-result envelope (spec §3 / Item 5)."""

import asyncio

import pytest

from core.steps import StepCost, StepResult, run_step


def test_ok_only_for_success_skipped_not_available() -> None:
    assert StepResult(step="s", status="success").ok
    assert StepResult(step="s", status="skipped").ok
    assert StepResult(step="s", status="not_available").ok
    assert not StepResult(step="s", status="failure").ok
    assert not StepResult(step="s", status="timeout").ok


async def test_run_step_success_returns_value_and_summary() -> None:
    async def work() -> dict[str, int]:
        return {"n": 3}

    result, value = await run_step("compute", work(), summarize=lambda v: f"n={v['n']}")
    assert result.status == "success" and result.ok
    assert value == {"n": 3}
    assert result.result_summary == "n=3"
    assert result.latency_ms >= 0


async def test_run_step_exception_becomes_failure_not_raised() -> None:
    async def boom() -> None:
        raise ValueError("nope")

    result, value = await run_step("risky", boom())
    assert result.status == "failure" and not result.ok
    assert value is None
    assert "ValueError: nope" in (result.error or "")


async def test_run_step_timeout_becomes_timeout_status() -> None:
    async def slow() -> int:
        await asyncio.sleep(5)
        return 1

    result, value = await run_step("slow", slow(), timeout_s=0.05)
    assert result.status == "timeout" and not result.ok and value is None


async def test_run_step_reraises_cancellation_for_barge_in() -> None:
    async def hang() -> None:
        await asyncio.sleep(10)

    task = asyncio.ensure_future(run_step("hang", hang()))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_trace_fields_flatten_cost_and_status() -> None:
    r = StepResult(
        step="llm",
        status="success",
        latency_ms=12.4,
        cost=StepCost(tokens_in=100, tokens_out=20, usd=0.001),
        result_summary="ok",
        detail={"model": "x"},
    )
    f = r.trace_fields()
    assert f["status"] == "success" and f["ok"] is True
    assert f["tokens_in"] == 100 and f["usd"] == 0.001
    assert f["model"] == "x"
