"""E2E for Phase 5 (§17+§18): session close → queued consolidation → learning.

The full slow loop over real infrastructure: a closed session's transcript
is queued (live path returns immediately), the worker consolidates —
Graphiti extracts the fact, the psych model absorbs the mood, patterns land
in procedural memory — and the fact is retrievable from semantic memory.
"""

import time
import uuid
from pathlib import Path

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from adapters.graph.graphiti import GraphitiGraphStore
from adapters.llm.openrouter import OpenRouterLLM
from adapters.queue.redis import RedisTaskQueue
from config.settings import get_settings
from core.cost import CostLedger
from core.memory.procedural import ProceduralMemory
from core.memory.semantic import SemanticMemory
from core.memory.working import Turn, WorkingMemory
from core.psych.consolidation import CONSOLIDATION_TASK_TYPE, Consolidator
from core.psych.user_model import PsychUserModel
from tests.integration.conftest import wait_until_healthy
from workers.task_worker import TaskWorker

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.integration,
    pytest.mark.skipif(
        not get_settings().open_router_api_key,
        reason="OPEN_ROUTER_API_KEY not set — consolidation needs a real LLM",
    ),
]

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"


async def test_session_close_learns_via_queued_consolidation() -> None:
    settings = get_settings()
    database = Database(settings)
    queue = RedisTaskQueue(settings, namespace=f"test_{uuid.uuid4().hex[:12]}")
    user_id = f"it_{uuid.uuid4().hex[:12]}"
    session = f"it_s_{uuid.uuid4().hex[:8]}"
    try:
        await wait_until_healthy(database)
        await queue._redis.delete(queue._queue_key)

        docs = MongoDocStore(database)
        ledger = CostLedger(docs)
        llm = OpenRouterLLM(settings, ledger=ledger)
        graph_store = GraphitiGraphStore(database, ledger=ledger)
        await graph_store.setup()
        semantic = SemanticMemory(graph_store)
        consolidator = Consolidator(
            semantic, ProceduralMemory(docs), PsychUserModel(docs), docs, llm
        )

        # A session happens and closes.
        wm = WorkingMemory()
        wm.append(
            session,
            Turn(
                role="user",
                text="by the way, I adopted a cat last month — her name is Waffles",
                emotion={"valence": 0.5, "arousal": 0.2},
            ),
        )
        wm.append(session, Turn(role="assistant", text="Waffles! that's a great name"))
        transcript = wm.close(session)

        # Session close enqueues consolidation; the live path returns instantly.
        started = time.perf_counter()
        await queue.enqueue(
            session_id=session,
            user_id=user_id,
            type=CONSOLIDATION_TASK_TYPE,
            params={"transcript": [t.model_dump() for t in transcript]},
        )
        assert (time.perf_counter() - started) < 0.5  # no LLM ran on the live path

        # The background worker does the slow loop.
        worker = TaskWorker(queue)
        worker.register(CONSOLIDATION_TASK_TYPE, consolidator.task_handler())
        assert await worker.run_once(timeout_s=2.0) is True

        pending = await queue.pending_deliveries(session)
        assert len(pending) == 1
        report = pending[0].result or {}
        assert report.get("facts_extracted") is True
        assert report.get("mood_updated") is True

        # The learned fact is retrievable from semantic memory.
        facts = await semantic.profile_facts(user_id, limit=10)
        texts = " | ".join(f.fact.lower() for f in facts)
        assert "waffles" in texts, f"cat fact missing from semantic memory: {texts}"

        # Mood baseline absorbed the session's signal.
        model = await PsychUserModel(docs).get(user_id)
        assert model.mood_baseline.samples == 1
        assert model.mood_baseline.valence == pytest.approx(0.5)
    finally:
        for collection in ("procedural", "psych_model", "psych_correlations", "cost_ledger"):
            await database.mongo(collection).delete_many({"user_id": user_id})
            await database.mongo(collection).delete_many({"_id": user_id})
        await queue.aclose()
        await database.aclose()
