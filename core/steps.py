"""Unified structured step-result envelope (spec §3/Item 5).

Every pipeline step that can succeed, be skipped, fail, time out, or be
unavailable — a tool call, an LLM call, a memory read/write, a search, a
reasoning/self-reflection step — reports its outcome as ONE `StepResult`. A
broken step becomes a clean ``status="failure"`` envelope (with the error text)
rather than a hang or a bare exception that tears down the turn, so the companion
can still respond and the failure is visible in the per-turn trace.

`run_step()` is the reusable wrapper: hand it a step name and an awaitable and it
produces the envelope — timing it, catching exceptions into ``failure``, and
mapping a timeout to ``timeout`` — so no call site has to hand-roll try/except.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field

StepStatus = Literal["success", "failure", "skipped", "timeout", "not_available"]

T = TypeVar("T")


class StepCost(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0


class StepResult(BaseModel):
    """One step's outcome, uniform across tools / LLM / memory / search / reasoning."""

    step: str
    status: StepStatus = "success"
    error: str | None = None
    latency_ms: float = 0.0
    cost: StepCost = Field(default_factory=StepCost)
    result_summary: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """A step 'went fine' if it succeeded or was legitimately skipped/absent —
        NOT if it failed or timed out."""
        return self.status in ("success", "skipped", "not_available")

    def trace_fields(self) -> dict[str, Any]:
        """Flattened for a trace span (stage=<step>)."""
        return {
            "status": self.status,
            "ok": self.ok,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 1),
            "tokens_in": self.cost.tokens_in,
            "tokens_out": self.cost.tokens_out,
            "usd": self.cost.usd,
            "result_summary": self.result_summary,
            **self.detail,
        }


async def run_step(
    step: str,
    coro: Awaitable[T],
    *,
    timeout_s: float | None = None,
    summarize: Callable[[T], str] | None = None,
    cost: StepCost | None = None,
) -> tuple[StepResult, T | None]:
    """Run one step; return its envelope + the raw value (None on failure/timeout).

    - success  → value returned, status="success", latency measured;
    - a raised exception → status="failure" with the error text (never re-raised,
      except CancelledError which must propagate for barge-in §24);
    - exceeding ``timeout_s`` → status="timeout".
    The caller inspects ``result.ok`` and decides how the companion responds — a
    broken step never hangs or leaks a bare traceback into the turn.
    """
    started = time.perf_counter()
    try:
        awaitable = asyncio.wait_for(coro, timeout_s) if timeout_s else coro
        value = await awaitable
    except asyncio.CancelledError:
        raise  # barge-in / shutdown — must propagate, not be swallowed
    except TimeoutError:
        return (
            StepResult(
                step=step,
                status="timeout",
                error=f"exceeded {timeout_s}s",
                latency_ms=(time.perf_counter() - started) * 1000,
            ),
            None,
        )
    except Exception as exc:
        return (
            StepResult(
                step=step,
                status="failure",
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000,
            ),
            None,
        )
    latency_ms = (time.perf_counter() - started) * 1000
    summary = ""
    if summarize is not None:
        try:
            summary = summarize(value)
        except Exception:  # a bad summarizer must not fail the step
            summary = ""
    return (
        StepResult(
            step=step,
            status="success",
            latency_ms=latency_ms,
            cost=cost or StepCost(),
            result_summary=summary,
        ),
        value,
    )
