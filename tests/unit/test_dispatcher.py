"""Unit tests for the Tool Dispatcher (spec §13) — queue and LLM faked."""

import asyncio
import json
from typing import Any

import pytest

from core.cost import COST_COLLECTION, CostLedger
from core.reasoning.prompt_assembly import AssembledPrompt
from core.tools.dispatcher import (
    ConfirmRequest,
    LoopOutcome,
    QueuedHandle,
    ToolCall,
    ToolDispatcher,
    ToolResult,
)
from core.tools.registry import ToolContext, ToolRegistry, ToolSpec
from tests.fakes import FakeDocStore, FakeLLM

CONTEXT = ToolContext(user_id="u_demo_001", session_id="s1")


class FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    async def enqueue(self, **kwargs: Any) -> str:
        self.enqueued.append(kwargs)
        return f"task-{len(self.enqueued)}"

    # Remaining TaskQueue protocol surface — unused by the dispatcher:
    async def status(self, task_id: str) -> Any:
        raise NotImplementedError

    async def pending_deliveries(self, session_id: str) -> list[Any]:
        raise NotImplementedError

    async def mark_delivered(self, task_id: str) -> None:
        raise NotImplementedError

    async def mark_suppressed(self, task_id: str) -> None:
        raise NotImplementedError

    async def claim_next(self, timeout_s: float = 1.0) -> Any:
        raise NotImplementedError

    async def complete(self, task_id: str, result: dict[str, Any]) -> None:
        raise NotImplementedError

    async def fail(self, task_id: str, error: str) -> None:
        raise NotImplementedError


def _registry() -> tuple[ToolRegistry, dict[str, int]]:
    registry = ToolRegistry()
    calls = {"time": 0, "research": 0, "trade": 0, "flaky": 0}

    async def get_time(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        calls["time"] += 1
        return {"time": "18:00"}

    async def deep_research(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        calls["research"] += 1
        return {"report": "long"}

    async def log_trade(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        calls["trade"] += 1
        return {"logged": args}

    async def variable_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        calls["flaky"] += 1
        await asyncio.sleep(args.get("sleep", 0))
        return {"ok": True}

    registry.register(
        ToolSpec(id="get_time", description="current time", type="readonly"), get_time
    )
    registry.register(
        ToolSpec(
            id="deep_research", description="slow research",
            type="background", latency_class="slow",
        ),
        deep_research,
    )
    registry.register(
        ToolSpec(
            id="log_trade", description="record a trade", type="action",
            requires_confirmation=True, interruptible=False,
            scope="project:finance_portfolio",
        ),
        log_trade,
    )
    registry.register(
        ToolSpec(id="variable_tool", description="maybe slow", latency_class="variable"),
        variable_tool,
    )
    registry.register(
        ToolSpec(
            id="garden_notes", description="garden tool", scope="project:garden_planner"
        ),
        get_time,
    )
    return registry, calls


def _prompt(utterance: str = "hi") -> AssembledPrompt:
    return AssembledPrompt(
        user_id="u_demo_001",
        session_id="s1",
        utterance=utterance,
        system_prompt="You are Companion.",
        messages=[
            {"role": "system", "content": "You are Companion."},
            {"role": "user", "content": utterance},
        ],
        complexity_hint="simple",
    )


# Acceptance: readonly runs inline, result feeds back into the same turn.
async def test_readonly_tool_runs_inline_and_feeds_back_same_turn() -> None:
    registry, calls = _registry()
    dispatcher = ToolDispatcher(registry, FakeQueue())
    llm = FakeLLM(
        [
            json.dumps({"action": "tool", "tool_id": "get_time", "args": {}}),
            json.dumps({"action": "final", "text": "it's 18:00 right now"}),
        ]
    )

    outcome = await dispatcher.loop(_prompt("what time is it?"), llm, CONTEXT)

    assert outcome.kind == "final" and outcome.text == "it's 18:00 right now"
    assert calls["time"] == 1
    assert outcome.tool_results[0].output == {"time": "18:00"}
    # The tool result was visible to the LLM before its final answer:
    assert "18:00" in json.dumps(llm.calls[1]["messages"])


# Acceptance: a slow tool is enqueued; conversation continues.
async def test_slow_tool_is_enqueued_and_conversation_continues() -> None:
    registry, calls = _registry()
    queue = FakeQueue()
    dispatcher = ToolDispatcher(registry, queue)
    llm = FakeLLM(
        [
            json.dumps({"action": "tool", "tool_id": "deep_research", "args": {"q": "x"}}),
            json.dumps({"action": "final", "text": "I kicked off research; more soon."}),
        ]
    )

    outcome = await dispatcher.loop(_prompt("research this deeply"), llm, CONTEXT)

    assert outcome.kind == "final"
    assert calls["research"] == 0  # ran nowhere near the live path
    assert len(outcome.queued) == 1
    assert queue.enqueued[0]["type"] == "tool"
    assert queue.enqueued[0]["params"]["tool_id"] == "deep_research"


# Acceptance: an action tool requests confirmation before executing.
async def test_action_tool_requires_confirmation_then_executes() -> None:
    registry, calls = _registry()
    dispatcher = ToolDispatcher(registry, FakeQueue())
    call = ToolCall(tool_id="log_trade", args={"ticker": "SYPNL", "qty": 5})

    first = await dispatcher.dispatch(call, CONTEXT)
    assert isinstance(first, ConfirmRequest)
    assert calls["trade"] == 0

    second = await dispatcher.dispatch(call, CONTEXT, confirmed=True)
    assert isinstance(second, ToolResult)
    assert calls["trade"] == 1
    assert second.output["logged"]["ticker"] == "SYPNL"


# Acceptance: only the referenced project's tools (plus core) are injected.
async def test_only_context_scoped_tools_are_offered_to_the_llm() -> None:
    registry, _ = _registry()
    dispatcher = ToolDispatcher(registry, FakeQueue())
    llm = FakeLLM([json.dumps({"action": "final", "text": "ok"})])
    context = ToolContext(
        user_id="u_demo_001", session_id="s1", project_type="finance_portfolio"
    )

    await dispatcher.loop(_prompt(), llm, context)

    offered = json.dumps(llm.calls[0]["messages"])
    assert "log_trade" in offered and "get_time" in offered
    assert "garden_notes" not in offered  # other project's tool never leaks


# Acceptance: a variable tool that overruns its budget is promoted to the queue.
async def test_variable_tool_overrunning_budget_is_promoted() -> None:
    registry, _ = _registry()
    queue = FakeQueue()
    dispatcher = ToolDispatcher(registry, queue, variable_budget_s=0.05)

    fast = await dispatcher.dispatch(
        ToolCall(tool_id="variable_tool", args={"sleep": 0}), CONTEXT
    )
    assert isinstance(fast, ToolResult)

    slow = await dispatcher.dispatch(
        ToolCall(tool_id="variable_tool", args={"sleep": 0.3}), CONTEXT
    )
    assert isinstance(slow, QueuedHandle)
    assert queue.enqueued[0]["params"]["tool_id"] == "variable_tool"


# Rule 6: an in-flight action tool must not be cancelled mid-execution.
async def test_confirmed_action_survives_cancellation_of_the_caller() -> None:
    registry = ToolRegistry()
    finished = asyncio.Event()

    async def slow_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        await asyncio.sleep(0.1)
        finished.set()
        return {"written": True}

    registry.register(
        ToolSpec(id="write_thing", description="w", type="action", interruptible=False),
        slow_write,
    )
    dispatcher = ToolDispatcher(registry, FakeQueue())

    outer = asyncio.create_task(
        dispatcher.dispatch(ToolCall(tool_id="write_thing"), CONTEXT, confirmed=True)
    )
    await asyncio.sleep(0.02)
    outer.cancel()  # barge-in
    with pytest.raises(asyncio.CancelledError):
        await outer

    await asyncio.wait_for(finished.wait(), timeout=1.0)  # the write still completed


async def test_every_dispatch_logs_a_ledger_entry() -> None:
    registry, _ = _registry()
    docs = FakeDocStore()
    ledger = CostLedger(docs)
    dispatcher = ToolDispatcher(registry, FakeQueue(), ledger=ledger)

    await dispatcher.dispatch(ToolCall(tool_id="get_time"), CONTEXT)
    await ledger.flush()

    (row,) = await docs.find(COST_COLLECTION)
    assert row["component"] == "tool"
    assert row["metadata"]["task_id"] == "get_time"
    assert row["user_id"] == "u_demo_001"


async def test_unparseable_loop_steps_end_with_safe_final() -> None:
    registry, _ = _registry()
    dispatcher = ToolDispatcher(registry, FakeQueue())
    llm = FakeLLM(["garbage", "more garbage"])

    outcome = await dispatcher.loop(_prompt(), llm, CONTEXT)

    assert isinstance(outcome, LoopOutcome)
    assert outcome.kind == "final" and outcome.text
