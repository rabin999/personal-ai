"""E2E for Phase 3 (§9-§12): one real text conversation turn.

The full path: memory written in a past session → prompt assembly over real
stores → OpenRouter generation with validated judgment → behavior gates →
self-model log → cost ledger. This is the "remembers me and talks like a
person in text" milestone the build order targets before voice.
"""

import uuid
from pathlib import Path

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from adapters.graph.graphiti import GraphitiGraphStore
from adapters.llm.openrouter import OpenRouterLLM
from adapters.vector.qdrant import QdrantVectorStore
from config.settings import get_settings
from core.cost import CostLedger
from core.memory.entities import EntityResolver
from core.memory.episodic import EpisodicMemory
from core.memory.procedural import ProceduralMemory
from core.memory.semantic import SemanticMemory
from core.memory.working import Turn, WorkingMemory
from core.profile import ProfileService, TraitRegistry
from core.reasoning.prompt_assembly import PromptAssembler
from core.reasoning.response_gen import ResponseGenerator
from core.reasoning.self_model import SelfModel
from tests.integration.conftest import wait_until_healthy

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.integration,
    pytest.mark.skipif(
        not get_settings().open_router_api_key,
        reason="OPEN_ROUTER_API_KEY not set — full turn needs a real LLM",
    ),
]

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"


async def test_full_text_conversation_turn() -> None:
    settings = get_settings()
    database = Database(settings)
    user_id = f"it_{uuid.uuid4().hex[:12]}"
    session = f"s_{uuid.uuid4().hex[:8]}"
    try:
        await wait_until_healthy(database)
        await database.startup()

        docs = MongoDocStore(database)
        vectors = QdrantVectorStore(database, settings.embedding_model)
        ledger = CostLedger(docs)
        profiles = ProfileService(docs)
        registry = TraitRegistry(docs, profiles)
        await registry.seed_defaults(DEFAULTS_DIR)
        working = WorkingMemory()
        self_model = SelfModel(docs, vectors, OpenRouterLLM(settings, ledger=ledger))
        assembler = PromptAssembler(
            profiles,
            registry,
            working,
            EpisodicMemory(vectors),
            SemanticMemory(GraphitiGraphStore(database, ledger=ledger)),
            ProceduralMemory(docs),
            EntityResolver(vectors),
            self_model,
        )
        generator = ResponseGenerator(
            OpenRouterLLM(settings, ledger=ledger), self_model, registry
        )

        # A past session left episodic memory behind.
        episodic = EpisodicMemory(vectors)
        await episodic.write(
            user_id,
            "s_previous",
            ["user: I finally bought 20 shares of SYPNL at 42 dollars\n"
             "assistant: nice — that's the biotech ticker you'd been watching"],
        )

        # Current session: one real turn.
        working.append(session, Turn(role="user", text="hey, good evening"))
        working.append(session, Turn(role="assistant", text="hey! good to hear you"))

        prompt = await assembler.assemble(
            user_id, session, "remind me which stock I bought last week and how many shares?"
        )
        result = await generator.generate(prompt)

        # The reply used memory, the gates ran, and the turn left its traces.
        assert result.action in ("respond", "clarify", "curious_followup")
        assert result.final_text.strip()
        assert "sypnl" in result.final_text.lower()

        log_entry = await database.mongo("self_model_log").find_one({"_id": result.turn_id})
        assert log_entry is not None and log_entry["user_id"] == user_id

        await ledger.flush()
        spend = await ledger.get(user_id, component="llm")
        assert spend.count >= 1  # generation (+ any rewrite) hit the ledger
    finally:
        await database.mongo("self_model_log").delete_many({"user_id": user_id})
        await database.mongo("cost_ledger").delete_many({"user_id": user_id})
        await database.mongo("user_profile").delete_many({"_id": user_id})
        await database.aclose()
