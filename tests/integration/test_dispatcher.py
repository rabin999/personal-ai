"""Integration tests for the Tool Dispatcher (spec §13) — real Redis queue."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from adapters.queue.redis import RedisTaskQueue
from config.settings import Settings
from core.tools.dispatcher import QueuedHandle, ToolCall, ToolDispatcher
from core.tools.registry import ToolContext, ToolRegistry, ToolSpec
from workers.task_worker import TaskWorker

pytestmark = pytest.mark.integration


@pytest.fixture
async def queue() -> AsyncIterator[RedisTaskQueue]:
    q = RedisTaskQueue(Settings(_env_file=None), namespace=f"test_{uuid.uuid4().hex[:12]}")
    await q._redis.delete(q._queue_key)
    yield q
    await q.aclose()


async def test_promoted_tool_executes_on_the_worker_and_delivers(
    queue: RedisTaskQueue,
) -> None:
    registry = ToolRegistry()
    ran: list[dict[str, Any]] = []

    async def deep_research(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        ran.append(args)
        return {"report": f"findings on {args.get('topic')}"}

    registry.register(
        ToolSpec(
            id="deep_research",
            description="slow research",
            type="background",
            latency_class="slow",
        ),
        deep_research,
    )
    dispatcher = ToolDispatcher(registry, queue)
    context = ToolContext(
        user_id=f"it_{uuid.uuid4().hex[:12]}", session_id=f"it_s_{uuid.uuid4().hex[:8]}"
    )

    handle = await dispatcher.dispatch(
        ToolCall(tool_id="deep_research", args={"topic": "SYPNL"}), context
    )
    assert isinstance(handle, QueuedHandle)
    assert ran == []  # nothing ran on the live path

    worker = TaskWorker(queue)
    worker.register("tool", dispatcher.task_handler())
    assert await worker.run_once() is True

    assert ran == [{"topic": "SYPNL"}]
    task = await queue.status(handle.task_id)
    assert task.status == "completed"
    assert task.result == {"report": "findings on SYPNL"}
    pending = await queue.pending_deliveries(context.session_id)
    assert [t.task_id for t in pending] == [handle.task_id]
