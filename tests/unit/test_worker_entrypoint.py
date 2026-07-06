"""Unit test for the background worker wiring (spec §14/§18) — pipeline faked.

Verifies the deployable entrypoint registers every background task type against
the queue, and that the worker dispatches a queued task to the right handler.
"""

from types import SimpleNamespace
from typing import Any

from core.psych.consolidation import CONSOLIDATION_TASK_TYPE
from core.tools.dispatcher import TOOL_TASK_TYPE
from ports.queue import QueuedTask
from workers.consolidation_worker import WEB_SEARCH_TASK_TYPE, build_worker


class FakeQueue:
    def __init__(self, task: QueuedTask | None = None) -> None:
        self._task = task
        self.completed: dict[str, dict[str, Any]] = {}

    async def claim_next(self, timeout_s: float = 1.0) -> QueuedTask | None:
        task, self._task = self._task, None
        return task

    async def complete(self, task_id: str, result: dict[str, Any]) -> None:
        self.completed[task_id] = result

    async def fail(self, task_id: str, error: str) -> None:  # pragma: no cover
        raise AssertionError(f"unexpected fail: {error}")


def _handler(tag: str) -> Any:
    async def handle(task: QueuedTask) -> dict[str, Any]:
        return {"handled_by": tag, "type": task.type}

    return handle


def _pipeline(queue: FakeQueue) -> Any:
    return SimpleNamespace(
        queue=queue,
        dispatcher=SimpleNamespace(task_handler=lambda: _handler("dispatcher")),
        web_search=SimpleNamespace(as_task_handler=lambda: _handler("web_search")),
        consolidator=SimpleNamespace(task_handler=lambda: _handler("consolidator")),
    )


def test_worker_registers_all_background_task_types() -> None:
    worker = build_worker(_pipeline(FakeQueue()))
    assert worker.task_types() == sorted(
        [TOOL_TASK_TYPE, WEB_SEARCH_TASK_TYPE, CONSOLIDATION_TASK_TYPE]
    )


async def test_worker_dispatches_consolidation_to_its_handler() -> None:
    task = QueuedTask(
        task_id="t1",
        session_id="s1",
        user_id="u_demo_001",
        type=CONSOLIDATION_TASK_TYPE,
        params={},
        created_at="2026-07-06T00:00:00Z",
    )
    queue = FakeQueue(task)
    worker = build_worker(_pipeline(queue))

    assert await worker.run_once() is True
    assert queue.completed["t1"]["handled_by"] == "consolidator"
