"""Port: background task queue (Redis adapter) — pull-at-pause delivery (spec §14).

Slow work runs off the conversation path. Results are never pushed: the
conversation layer pulls ``pending_deliveries`` at a natural pause and asks
the LLM to compose the interjection (or suppresses it if the user moved on).
"""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

TaskStatus = Literal["queued", "running", "completed", "failed"]
DeliveryState = Literal["pending", "delivered", "suppressed"]


class QueuedTask(BaseModel):
    task_id: str
    session_id: str
    user_id: str
    type: str
    params: dict[str, Any] = Field(default_factory=dict)
    status: TaskStatus = "queued"
    result: dict[str, Any] | None = None
    error: str | None = None
    delivery_state: DeliveryState = "pending"
    created_at: str
    resolved_at: str | None = None


class TaskQueue(Protocol):
    async def enqueue(
        self, *, session_id: str, user_id: str, type: str, params: dict[str, Any]
    ) -> str:
        """Queue a task; returns its task_id immediately."""
        ...

    async def status(self, task_id: str) -> QueuedTask: ...

    async def pending_deliveries(self, session_id: str) -> list[QueuedTask]:
        """Completed tasks for this session that were not yet delivered."""
        ...

    async def mark_delivered(self, task_id: str) -> None: ...

    async def mark_suppressed(self, task_id: str) -> None:
        """Result dropped because the user moved on (rule 2)."""
        ...

    # Worker side:

    async def claim_next(self, timeout_s: float = 1.0) -> QueuedTask | None:
        """Pop the next queued task (blocking up to timeout); None on empty."""
        ...

    async def complete(self, task_id: str, result: dict[str, Any]) -> None: ...

    async def fail(self, task_id: str, error: str) -> None: ...
