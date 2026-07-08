"""Pull-at-pause delivery (spec §14 rule 2).

At a natural conversation pause, resolved background tasks are pulled and
each result is handed to the LLM to compose a fresh interjection — never a
template. The LLM also judges relevance: if the user has moved on, the
result is suppressed instead of spoken.
"""

import json
import logging
from datetime import UTC, datetime

from pydantic import BaseModel, ValidationError

from ports.llm import LLM, LLMUnavailable
from ports.queue import QueuedTask, TaskQueue

logger = logging.getLogger(__name__)

# Time-sensitive results (news/scores/weather/"today") go stale and must be DROPPED
# rather than delivered late at the next conversation open (brief U9 staleness check).
_STALE_AFTER_S = 6 * 3600
_TIME_SENSITIVE_HINTS = (
    "today",
    "now",
    "current",
    "latest",
    "news",
    "score",
    "weather",
    "open",
    "price",
    "tonight",
)

_COMPOSE_INSTRUCTIONS = (
    "A background task you started for the user earlier has finished. Given its "
    "result and the recent conversation, decide whether it's still relevant, and "
    "if so, deliver the ACTUAL finding in one short natural spoken line — state "
    "the key fact from the result itself, do NOT just say it's 'ready' or "
    "'done'. E.g. 'Market's open till 3 today' — not 'that market info is ready.' "
    "Respond ONLY with JSON: "
    '{"relevant": true|false, "line": "<the finding, in your companion voice>"}. '
    "If the user has clearly moved on or the result no longer matters, set "
    "relevant to false."
)


def _is_stale(task: QueuedTask) -> bool:
    """True when a time-sensitive result has aged past the staleness window (U9)."""
    blob = f"{task.type} {json.dumps(task.params)}".lower()
    if not any(hint in blob for hint in _TIME_SENSITIVE_HINTS):
        return False
    stamp = task.resolved_at or task.created_at
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return False
    return age > _STALE_AFTER_S


class _Composed(BaseModel):
    relevant: bool
    line: str = ""


class Interjection(BaseModel):
    task_id: str
    line: str


# Pileup cap (§8.8 / §14): never machine-gun a backlog of finished tasks at the
# user. Up to this many resolved results are spoken at a pause; beyond it, we
# summarize-and-offer ONE line instead of dumping them all.
MAX_INTERJECTIONS = 2


class DeliveryComposer:
    def __init__(
        self, queue: TaskQueue, llm: LLM, max_interjections: int = MAX_INTERJECTIONS
    ) -> None:
        self._queue = queue
        self._llm = llm
        self._max = max(1, max_interjections)

    async def deliveries_for_pause(
        self, session_id: str, user_id: str, recent_context: str
    ) -> list[Interjection]:
        """Compose interjections for resolved tasks; suppress stale ones; and cap
        the pileup so a backlog becomes a single summarize-and-offer, never a
        machine-gun of results (§8.8)."""
        relevant: list[Interjection] = []
        for task in await self._queue.pending_deliveries(session_id):
            line = await self._compose(user_id, task, recent_context)
            if line is None:  # stale / user moved on → purge, don't deliver
                await self._queue.mark_suppressed(task.task_id)
                continue
            relevant.append(Interjection(task_id=task.task_id, line=line))

        # Pileup: more finished than we'd ever say at once → mark them all
        # delivered (so they don't re-fire) and offer ONCE instead of dumping.
        if len(relevant) > self._max:
            for item in relevant:
                await self._queue.mark_delivered(item.task_id)
            n = len(relevant)
            offer = (
                f"Oh — while we were talking I finished {n} things you'd asked about. "
                "Want me to run through them?"
            )
            return [Interjection(task_id=relevant[0].task_id, line=offer)]

        for item in relevant:
            await self._queue.mark_delivered(item.task_id)
        return relevant

    async def deliveries_at_open(self, user_id: str, session_id: str) -> list[Interjection]:
        """Brief U9: at conversation OPEN, surface results that finished while the user
        was away (in a now-closed session) — 'oh hey, that thing you asked me to look
        up — I found it.' Time-sensitive results that have gone stale are DROPPED, not
        delivered late. Excludes the current session (its in-session path handles it).
        User-scoped; capped/offered like the pause path."""
        carried = await self._queue.pending_deliveries_for_user(user_id, exclude_session=session_id)
        relevant: list[Interjection] = []
        for task in carried:
            if _is_stale(task):  # expired time-sensitive result → drop silently
                await self._queue.mark_suppressed(task.task_id)
                continue
            # No live conversation context at open — compose against the request itself.
            line = await self._compose(user_id, task, "(the user just opened a new conversation)")
            if line is None:
                await self._queue.mark_suppressed(task.task_id)
                continue
            relevant.append(Interjection(task_id=task.task_id, line=line))
        if len(relevant) > self._max:
            for item in relevant:
                await self._queue.mark_delivered(item.task_id)
            offer = (
                f"Hey — while you were gone I finished {len(relevant)} things you'd asked "
                "about. Want me to run through them?"
            )
            return [Interjection(task_id=relevant[0].task_id, line=offer)]
        for item in relevant:
            await self._queue.mark_delivered(item.task_id)
        return relevant

    async def _compose(self, user_id: str, task: QueuedTask, recent_context: str) -> str | None:
        payload = json.dumps({"task_type": task.type, "params": task.params, "result": task.result})
        messages = [
            {"role": "system", "content": _COMPOSE_INSTRUCTIONS},
            {
                "role": "user",
                "content": f"Recent conversation:\n{recent_context}\n\nFinished task:\n{payload}",
            },
        ]
        for _ in range(2):  # validate; retry once (§0.5)
            try:
                result = await self._llm.complete(
                    user_id,
                    messages,
                    "simple",
                    response_format={"type": "json_object"},
                    session_id=task.session_id,
                    temperature=0.2,  # P2: relevance decision → low temp
                    purpose="delivery_relevance",
                )
                composed = _Composed.model_validate_json(result.text)
            except (LLMUnavailable, ValidationError, ValueError):
                continue
            if not composed.relevant or not composed.line.strip():
                return None
            return composed.line.strip()
        logger.warning("interjection composition failed twice; suppressing %s", task.task_id)
        return None
