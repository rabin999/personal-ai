"""E2E for Phase 4 (§13+§16): a project action through the real agentic loop.

Create a finance project → the LLM-driven tool loop proposes the log_entry
action → confirmation gate → confirmed execution writes the ledger →
metrics update → consent-gated insight → per-project cost attribution →
project context feeds prompt assembly (§10 step 6).
"""

import uuid
from pathlib import Path

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from adapters.llm.openrouter import OpenRouterLLM
from adapters.queue.redis import RedisTaskQueue
from adapters.vector.qdrant import QdrantVectorStore
from config.settings import get_settings
from core.cost import CostLedger
from core.memory.entities import EntityResolver
from core.profile import ProfileService, TraitRegistry
from core.projects.service import ProjectService
from core.reasoning.prompt_assembly import AssembledPrompt
from core.tools.dispatcher import ToolCall, ToolDispatcher, ToolResult
from core.tools.registry import ToolContext, ToolRegistry
from tests.integration.conftest import wait_until_healthy

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.integration,
    pytest.mark.skipif(
        not get_settings().open_router_api_key,
        reason="OPEN_ROUTER_API_KEY not set — the agentic loop needs a real LLM",
    ),
]

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"


async def test_project_action_flow_end_to_end() -> None:
    settings = get_settings()
    database = Database(settings)
    user_id = f"it_{uuid.uuid4().hex[:12]}"
    session = f"it_s_{uuid.uuid4().hex[:8]}"
    queue = RedisTaskQueue(settings, namespace=f"test_{uuid.uuid4().hex[:12]}")
    try:
        await wait_until_healthy(database)
        await database.startup()

        docs = MongoDocStore(database)
        ledger = CostLedger(docs)
        profiles = ProfileService(docs)
        await TraitRegistry(docs, profiles).seed_defaults(DEFAULTS_DIR)
        registry = ToolRegistry()
        entities = EntityResolver(QdrantVectorStore(database, settings.embedding_model))
        projects = ProjectService(
            docs, entities, registry, llm=OpenRouterLLM(settings, ledger=ledger)
        )
        dispatcher = ToolDispatcher(registry, queue, ledger=ledger)
        llm = OpenRouterLLM(settings, ledger=ledger)

        # Create the project: pointer indexed, action tool registered.
        project = await projects.create(user_id, "finance_portfolio", "NEPSE Portfolio")
        context = ToolContext(
            user_id=user_id,
            session_id=session,
            project_id=project.id,
            project_type="finance_portfolio",
        )

        # The LLM-driven loop should reach for the log_entry action → confirm gate.
        prompt = AssembledPrompt(
            user_id=user_id,
            session_id=session,
            utterance="log that I bought 20 shares of SYPNL at 42 dollars in my portfolio",
            system_prompt="You are Companion. Use the available tools for portfolio actions.",
            messages=[
                {
                    "role": "system",
                    "content": "You are Companion. Use the available "
                    "tools to perform portfolio actions the user asks for.",
                },
                {
                    "role": "user",
                    "content": "log that I bought 20 shares of SYPNL at 42 dollars in my portfolio",
                },
            ],
            complexity_hint="moderate",
        )
        outcome = await dispatcher.loop(prompt, llm, context)
        assert outcome.kind == "confirm", f"expected confirmation gate, got: {outcome}"
        assert outcome.confirm is not None
        assert outcome.confirm.tool_id == "finance_portfolio.log_entry"

        # User says yes → shielded execution writes the ledger.
        result = await dispatcher.dispatch(
            ToolCall(tool_id=outcome.confirm.tool_id, args=outcome.confirm.args),
            context,
            confirmed=True,
        )
        assert isinstance(result, ToolResult)

        state = await projects.state(project.id, user_id)
        assert state.metrics["positions"]["SYPNL"]["qty"] == 20
        assert state.metrics["net_invested"] == pytest.approx(840.0)

        # Consent-gated insight: pending first, spoken only after yes, with caveat.
        pending = await projects.pending_insight(project.id, user_id)
        assert pending is not None and pending.status == "pending"
        spoken = await projects.consent_and_deliver(pending.id, user_id)
        assert "not a financial advisor" in spoken

        # §10 step 6: project context is available for prompt assembly.
        context_text = await projects.project_context(user_id, project.id)
        assert context_text is not None and "SYPNL" in context_text

        # Rule 5: per-project cost is queryable.
        await ledger.flush()
        assert await ledger.project_spend(user_id, project.id) >= 0.0
        tool_costs = await ledger.get(user_id, component="tool", project_id=project.id)
        assert tool_costs.count >= 1
    finally:
        for collection in (
            "projects",
            "ledger_entries",
            "pending_insights",
            "cost_ledger",
            "self_model_log",
            "user_profile",
        ):
            await database.mongo(collection).delete_many({"user_id": user_id})
            await database.mongo(collection).delete_many({"_id": user_id})
        await queue.aclose()
        await database.aclose()
