"""Background task worker (spec §14): executes queued tasks off the
conversation path.

Handlers are registered per task type (web_search §15, tool promotion §13,
consolidation triggers §18). The worker claims tasks, runs the handler, and
records completed/failed — delivery back into conversation is pull-at-pause,
owned by the conversation layer.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ports.queue import QueuedTask, TaskQueue

logger = logging.getLogger(__name__)

Handler = Callable[[QueuedTask], Awaitable[dict[str, Any]]]


class TaskWorker:
    def __init__(self, queue: TaskQueue) -> None:
        self._queue = queue
        self._handlers: dict[str, Handler] = {}
        self._running = False

    def register(self, task_type: str, handler: Handler) -> None:
        self._handlers[task_type] = handler

    async def run_once(self, timeout_s: float = 1.0) -> bool:
        """Claim and execute one task; returns False when the queue is idle."""
        task = await self._queue.claim_next(timeout_s=timeout_s)
        if task is None:
            return False
        handler = self._handlers.get(task.type)
        if handler is None:
            await self._queue.fail(task.task_id, f"no handler for task type '{task.type}'")
            return True
        try:
            result = await handler(task)
        except Exception as exc:
            logger.exception("task %s (%s) failed", task.task_id, task.type)
            await self._queue.fail(task.task_id, f"{type(exc).__name__}: {exc}")
            return True
        await self._queue.complete(task.task_id, result)
        return True

    async def run_forever(self, idle_sleep_s: float = 0.5) -> None:
        self._running = True
        while self._running:
            worked = await self.run_once()
            if not worked:
                await asyncio.sleep(idle_sleep_s)

    def stop(self) -> None:
        self._running = False
