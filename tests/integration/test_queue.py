"""Integration tests for the Background Task Queue (spec §14) — real Redis."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest

from adapters.queue.redis import RedisTaskQueue
from config.settings import Settings
from ports.queue import QueuedTask
from workers.task_worker import TaskWorker

pytestmark = pytest.mark.integration


@pytest.fixture
async def queue() -> AsyncIterator[RedisTaskQueue]:
    q = RedisTaskQueue(Settings(_env_file=None), namespace=f"test_{uuid.uuid4().hex[:12]}")
    await q._redis.delete(q._queue_key)  # stale tasks from prior runs
    yield q
    await q.aclose()


@pytest.fixture
def session_id() -> str:
    return f"it_s_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def user_id() -> str:
    return f"it_{uuid.uuid4().hex[:12]}"


async def test_enqueue_execute_and_pull_at_pause_lifecycle(
    queue: RedisTaskQueue, session_id: str, user_id: str
) -> None:
    task_id = await queue.enqueue(
        session_id=session_id, user_id=user_id, type="web_search",
        params={"query": "latest SYPNL news"},
    )
    assert (await queue.status(task_id)).status == "queued"

    worker = TaskWorker(queue)

    async def search_handler(task: QueuedTask) -> dict[str, str]:
        return {"summary": f"results for {task.params['query']}"}

    worker.register("web_search", search_handler)
    assert await worker.run_once() is True

    task = await queue.status(task_id)
    assert task.status == "completed"
    assert task.result == {"summary": "results for latest SYPNL news"}
    assert task.resolved_at is not None

    pending = await queue.pending_deliveries(session_id)
    assert [t.task_id for t in pending] == [task_id]

    await queue.mark_delivered(task_id)
    assert await queue.pending_deliveries(session_id) == []
    assert (await queue.status(task_id)).delivery_state == "delivered"


async def test_queued_work_does_not_block_the_live_path(
    queue: RedisTaskQueue, session_id: str, user_id: str
) -> None:
    # Acceptance: a queued slow task resolves without blocking conversation.
    worker = TaskWorker(queue)

    async def slow_handler(task: QueuedTask) -> dict[str, str]:
        await asyncio.sleep(0.3)
        return {"done": "yes"}

    worker.register("slow_research", slow_handler)

    task_id = await queue.enqueue(
        session_id=session_id, user_id=user_id, type="slow_research", params={}
    )
    worker_run = asyncio.create_task(worker.run_once())

    # The "conversation" keeps making progress while the task runs.
    ticks = 0
    while not worker_run.done():
        ticks += 1
        await asyncio.sleep(0.02)
    await worker_run

    assert ticks >= 5  # live path kept ticking during execution
    assert (await queue.status(task_id)).status == "completed"


async def test_failed_handler_marks_task_failed(
    queue: RedisTaskQueue, session_id: str, user_id: str
) -> None:
    worker = TaskWorker(queue)

    async def broken(task: QueuedTask) -> dict[str, str]:
        raise RuntimeError("provider exploded")

    worker.register("web_search", broken)
    task_id = await queue.enqueue(
        session_id=session_id, user_id=user_id, type="web_search", params={}
    )
    await worker.run_once()

    task = await queue.status(task_id)
    assert task.status == "failed"
    assert task.error is not None and "provider exploded" in task.error
    assert await queue.pending_deliveries(session_id) == []  # failures aren't delivered


async def test_unknown_task_type_fails_loudly(
    queue: RedisTaskQueue, session_id: str, user_id: str
) -> None:
    worker = TaskWorker(queue)
    task_id = await queue.enqueue(
        session_id=session_id, user_id=user_id, type="mystery", params={}
    )
    await worker.run_once()
    task = await queue.status(task_id)
    assert task.status == "failed" and "no handler" in (task.error or "")


async def test_sessions_do_not_see_each_others_deliveries(
    queue: RedisTaskQueue, user_id: str
) -> None:
    session_a, session_b = f"it_s_{uuid.uuid4().hex[:8]}", f"it_s_{uuid.uuid4().hex[:8]}"
    task_id = await queue.enqueue(
        session_id=session_a, user_id=user_id, type="web_search", params={}
    )
    await queue.complete(task_id, {"summary": "x"})

    assert [t.task_id for t in await queue.pending_deliveries(session_a)] == [task_id]
    assert await queue.pending_deliveries(session_b) == []
