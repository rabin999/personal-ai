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
    coros = [
        worker.run_forever(),
        outbox_worker.run_forever(),
        _route_memory_forever(pipeline),
    ]
    # §8.12: this process OWNS phrase regeneration in production (the edge only reads the store).
    if pipeline.settings.phrases_dynamic_enabled:
        from core.phrases.refresh import regenerate_forever

        coros.append(
            regenerate_forever(
                pipeline.phrase_generator,
                pipeline.phrase_store,
                pipeline.phrases,
                pipeline.settings.phrase_regen_interval_s,
            )
        )
        logger.info(
            "phrase regeneration running (every %.0fs)", pipeline.settings.phrase_regen_interval_s
        )
    try:
        await asyncio.gather(*coros)
    finally:
        await pipeline.aclose()


async def _route_memory_forever(pipeline: Pipeline) -> None:
    """Poll the raw log for unrouted turns and route them to long-term memory via
    the cursor (Item 9) — off the conversation path, exactly once per turn."""
    poll_s = pipeline.settings.memory_routing_poll_s
    while True:
        try:
            n = await pipeline.memory_router.route_pending()
            if n:
                logger.info("routed %d raw turn(s) to long-term memory", n)
        except Exception:  # never let the router loop die
            logger.exception("memory routing poll failed")
        await asyncio.sleep(poll_s)


if __name__ == "__main__":
    asyncio.run(main())
