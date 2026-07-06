"""Pull-at-pause delivery (spec §14 rule 2).

At a natural conversation pause, resolved background tasks are pulled and
each result is handed to the LLM to compose a fresh interjection — never a
template. The LLM also judges relevance: if the user has moved on, the
result is suppressed instead of spoken.
"""

import json
import logging

from pydantic import BaseModel, ValidationError

from ports.llm import LLM, LLMUnavailable
from ports.queue import QueuedTask, TaskQueue

logger = logging.getLogger(__name__)

_COMPOSE_INSTRUCTIONS = (
    "A background task you started for the user earlier has finished. "
    "Given its result and the recent conversation, decide whether it is "
    "still relevant. Respond ONLY with JSON: "
    '{"relevant": true|false, "line": "<one short natural spoken interjection '
    "delivering the result, in your companion voice — only when relevant>\"}. "
    "If the user has clearly moved on or the result no longer matters, set "
    'relevant to false.'
)


class _Composed(BaseModel):
    relevant: bool
    line: str = ""


class Interjection(BaseModel):
    task_id: str
    line: str


class DeliveryComposer:
    def __init__(self, queue: TaskQueue, llm: LLM) -> None:
        self._queue = queue
        self._llm = llm

    async def deliveries_for_pause(
        self, session_id: str, user_id: str, recent_context: str
    ) -> list[Interjection]:
        """Compose interjections for resolved tasks; suppress stale ones."""
        interjections: list[Interjection] = []
        for task in await self._queue.pending_deliveries(session_id):
            line = await self._compose(user_id, task, recent_context)
            if line is None:
                await self._queue.mark_suppressed(task.task_id)
                continue
            await self._queue.mark_delivered(task.task_id)
            interjections.append(Interjection(task_id=task.task_id, line=line))
        return interjections

    async def _compose(
        self, user_id: str, task: QueuedTask, recent_context: str
    ) -> str | None:
        payload = json.dumps(
            {"task_type": task.type, "params": task.params, "result": task.result}
        )
        messages = [
            {"role": "system", "content": _COMPOSE_INSTRUCTIONS},
            {
                "role": "user",
                "content": f"Recent conversation:\n{recent_context}\n\n"
                f"Finished task:\n{payload}",
            },
        ]
        for _ in range(2):  # validate; retry once (§0.5)
            try:
                result = await self._llm.complete(
                    user_id, messages, "simple", response_format={"type": "json_object"},
                    session_id=task.session_id,
                )
                composed = _Composed.model_validate_json(result.text)
            except (LLMUnavailable, ValidationError, ValueError):
                continue
            if not composed.relevant or not composed.line.strip():
                return None
            return composed.line.strip()
        logger.warning("interjection composition failed twice; suppressing %s", task.task_id)
        return None
