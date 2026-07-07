"""Background worker process (spec §3 architecture, §14 queue).

Runs off the conversation-latency path: claims queued tasks and executes them
through the same wired object graph the serving edge uses (``api.composition``
is the single composition root). Registers every background task type:

- ``tool``          — §13 tool calls promoted off the live budget
- ``web_search``    — §15 background search + summarize
- ``consolidation`` — §18 post-session learning (rules, mood, correlations)

Delivery of results back into conversation is pull-at-pause, owned by the
conversation layer (§14) — this process only produces the results.

Run:  uv run python -m workers.consolidation_worker
"""

import asyncio
import logging

from api.composition import Pipeline, build_pipeline
from config.settings import get_settings
from core.psych.consolidation import CONSOLIDATION_TASK_TYPE
from core.tools.dispatcher import TOOL_TASK_TYPE
from workers.outbox_worker import OutboxWorker
from workers.task_worker import TaskWorker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("workers.consolidation")

WEB_SEARCH_TASK_TYPE = "web_search"


def build_worker(pipeline: Pipeline) -> TaskWorker:
    """Register every background task type against the wired pipeline."""
    worker = TaskWorker(pipeline.queue)
    worker.register(TOOL_TASK_TYPE, pipeline.dispatcher.task_handler())
    worker.register(WEB_SEARCH_TASK_TYPE, pipeline.web_search.as_task_handler())
    worker.register(CONSOLIDATION_TASK_TYPE, pipeline.consolidator.task_handler())
    return worker


async def main() -> None:
    pipeline = await build_pipeline(get_settings())
    worker = build_worker(pipeline)
    outbox_worker = OutboxWorker(pipeline.outbox, pipeline.mailer)
    logger.info(
        "worker started — handling: %s + outbox",
        [TOOL_TASK_TYPE, WEB_SEARCH_TASK_TYPE, CONSOLIDATION_TASK_TYPE],
    )
    try:
        # Queue worker + outbox poller run concurrently in the worker process.
        await asyncio.gather(worker.run_forever(), outbox_worker.run_forever())
    finally:
        await pipeline.aclose()


if __name__ == "__main__":
    asyncio.run(main())
