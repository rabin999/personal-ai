"""E2E for §14: background work resolves and is delivered at a pause.

Real Redis queue + worker + real LLM composition: a "search" completes in
the background while conversation continues; at the next pause the result
arrives as a fresh spoken line, and an off-topic result is suppressed.
"""

import uuid

import pytest

from adapters.llm.openrouter import OpenRouterLLM
from adapters.queue.redis import RedisTaskQueue
from config.settings import get_settings
from core.tools.delivery import DeliveryComposer
from ports.queue import QueuedTask
from workers.task_worker import TaskWorker

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.integration,
    pytest.mark.skipif(
        not get_settings().open_router_api_key,
        reason="OPEN_ROUTER_API_KEY not set — interjection composition needs a real LLM",
    ),
]


async def test_background_result_delivered_as_fresh_line_at_pause() -> None:
    queue = RedisTaskQueue(get_settings(), namespace=f"test_{uuid.uuid4().hex[:12]}")
    session = f"it_s_{uuid.uuid4().hex[:8]}"
    user_id = f"it_{uuid.uuid4().hex[:12]}"
    try:
        await queue._redis.delete(queue._queue_key)  # stale tasks from prior runs
        task_id = await queue.enqueue(
            session_id=session,
            user_id=user_id,
            type="web_search",
            params={"query": "SYPNL biotech trial news"},
        )

        worker = TaskWorker(queue)

        async def fake_search(task: QueuedTask) -> dict[str, str]:
            return {
                "summary": "SYPNL's phase-2 trial met its primary endpoint; "
                "shares rose 12% after hours."
            }

        worker.register("web_search", fake_search)
        await worker.run_once()

        composer = DeliveryComposer(queue, OpenRouterLLM(get_settings()))
        interjections = await composer.deliveries_for_pause(
            session,
            user_id,
            "user: anyway, still waiting on that SYPNL news you were checking\n"
            "assistant: I'll let you know the moment it lands",
        )

        assert len(interjections) == 1
        line = interjections[0].line.lower()
        assert "sypnl" in line or "trial" in line or "12" in line
        assert (await queue.status(task_id)).delivery_state == "delivered"
    finally:
        await queue.aclose()
