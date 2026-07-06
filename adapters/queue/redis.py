"""Adapter: Redis task queue (implements ports.queue.TaskQueue, spec §14).

Task records live as JSON strings under ``task:{id}``; the work queue is a
Redis list (LPUSH/BRPOP); each session keeps a set of its task ids so
pull-at-pause delivery scans only that session's tasks.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

from config.settings import Settings
from ports.queue import QueuedTask

_QUEUE_KEY = "companion:tasks:queued"
_TASK_KEY = "companion:task:{task_id}"
_SESSION_KEY = "companion:session_tasks:{session_id}"

# Tasks expire after a week; delivery is conversational, not archival.
_TASK_TTL_S = 7 * 24 * 3600


def _now() -> str:
    return datetime.now(UTC).isoformat()


class RedisTaskQueue:
    def __init__(self, settings: Settings) -> None:
        self._redis: aioredis.Redis = aioredis.from_url(
            settings.redis_url, decode_responses=True
        )

    async def enqueue(
        self, *, session_id: str, user_id: str, type: str, params: dict[str, Any]
    ) -> str:
        task = QueuedTask(
            task_id=str(uuid.uuid4()),
            session_id=session_id,
            user_id=user_id,
            type=type,
            params=params,
            created_at=_now(),
        )
        await self._save(task)
        await self._redis.sadd(_SESSION_KEY.format(session_id=session_id), task.task_id)
        await self._redis.expire(_SESSION_KEY.format(session_id=session_id), _TASK_TTL_S)
        await self._redis.lpush(_QUEUE_KEY, task.task_id)
        return task.task_id

    async def status(self, task_id: str) -> QueuedTask:
        return await self._load(task_id)

    async def pending_deliveries(self, session_id: str) -> list[QueuedTask]:
        task_ids = await self._redis.smembers(_SESSION_KEY.format(session_id=session_id))
        tasks = []
        for task_id in task_ids:
            raw = await self._redis.get(_TASK_KEY.format(task_id=task_id))
            if raw is None:
                continue
            task = QueuedTask.model_validate_json(raw)
            if task.status == "completed" and task.delivery_state == "pending":
                tasks.append(task)
        tasks.sort(key=lambda t: t.created_at)
        return tasks

    async def mark_delivered(self, task_id: str) -> None:
        await self._set_delivery(task_id, "delivered")

    async def mark_suppressed(self, task_id: str) -> None:
        await self._set_delivery(task_id, "suppressed")

    async def claim_next(self, timeout_s: float = 1.0) -> QueuedTask | None:
        popped = await self._redis.brpop([_QUEUE_KEY], timeout=timeout_s)
        if popped is None:
            return None
        _, task_id = popped
        task = await self._load(str(task_id))
        task.status = "running"
        await self._save(task)
        return task

    async def complete(self, task_id: str, result: dict[str, Any]) -> None:
        task = await self._load(task_id)
        task.status = "completed"
        task.result = result
        task.resolved_at = _now()
        await self._save(task)

    async def fail(self, task_id: str, error: str) -> None:
        task = await self._load(task_id)
        task.status = "failed"
        task.error = error
        task.resolved_at = _now()
        await self._save(task)

    async def aclose(self) -> None:
        await self._redis.aclose()

    async def _set_delivery(self, task_id: str, state: str) -> None:
        task = await self._load(task_id)
        task.delivery_state = state  # type: ignore[assignment]
        await self._save(task)

    async def _save(self, task: QueuedTask) -> None:
        await self._redis.set(
            _TASK_KEY.format(task_id=task.task_id), task.model_dump_json(), ex=_TASK_TTL_S
        )

    async def _load(self, task_id: str) -> QueuedTask:
        raw = await self._redis.get(_TASK_KEY.format(task_id=task_id))
        if raw is None:
            raise KeyError(f"unknown task {task_id}")
        return QueuedTask.model_validate_json(raw)
