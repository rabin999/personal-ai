"""Integration tests for Prompt Assembly (spec §10) — real Mongo/Qdrant/Neo4j.

No paid calls: episodic/entity legs use local embeddings, the semantic legs
run real Graphiti hybrid search over whatever the graph already holds.
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from qdrant_client import models

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from adapters.graph.graphiti import GraphitiGraphStore
from adapters.vector.qdrant import QdrantVectorStore
from config.settings import Settings, get_settings
from core.memory.entities import EntityResolver
from core.memory.episodic import EpisodicMemory
from core.memory.procedural import ProceduralMemory
from core.memory.semantic import SemanticMemory
from core.memory.working import Turn, WorkingMemory
from core.profile import ProfileService, TraitRegistry
from core.reasoning.prompt_assembly import (
    AssembledPrompt,
    DisambiguationRequest,
    PromptAssembler,
)
from core.reasoning.self_model import SelfModel

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not get_settings().open_router_api_key,
        reason="OPEN_ROUTER_API_KEY not set — Graphiti search config needs a key at init",
    ),
]

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"


@pytest.fixture
def user_id() -> str:
    return f"it_{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def assembler(db: Database) -> AsyncIterator[tuple[PromptAssembler, WorkingMemory, object]]:
    settings = Settings(_env_file=None, open_router_api_key=get_settings().open_router_api_key)
    await db.ensure_qdrant_collections()
    docs = MongoDocStore(db)
    vectors = QdrantVectorStore(db, settings.embedding_model)
    profiles = ProfileService(docs)
    registry = TraitRegistry(docs, profiles)
    await registry.seed_defaults(DEFAULTS_DIR)
    working = WorkingMemory()
    entities = EntityResolver(vectors)
    built = PromptAssembler(
        profiles,
        registry,
        working,
        EpisodicMemory(vectors),
        SemanticMemory(GraphitiGraphStore(db)),
        ProceduralMemory(docs),
        entities,
        SelfModel(docs, vectors),
    )
    yield built, working, entities

    for collection in ("user_profile", "procedural"):
        await db.mongo(collection).delete_many({"_id": {"$regex": "^it_"}})
        await db.mongo(collection).delete_many({"user_id": {"$regex": "^it_"}})
    for qcol in ("episodic", "entities"):
        await db.qdrant().delete(
            qcol,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="user_id", match=models.MatchText(text="it_")
                        )
                    ]
                )
            ),
        )


async def test_full_assembly_over_real_stores(
    assembler: tuple[PromptAssembler, WorkingMemory, EntityResolver], user_id: str
) -> None:
    built, working, entities = assembler
    session = f"s_{uuid.uuid4().hex[:8]}"

    await entities.index(
        user_id, "project", "proj_nepse", "NEPSE Portfolio",
        "stock trading and investments on the Nepal stock exchange",
    )
    episodic = built._episodic
    await episodic.write(
        user_id, "s_old", ["user: I bought 20 shares of SYPNL at 42 last week"]
    )
    working.append(session, Turn(role="user", text="markets were wild today"))

    result = await built.assemble(user_id, session, "how is my NEPSE Portfolio doing?")

    assert isinstance(result, AssembledPrompt)
    assert result.resolved_entities[0].entity_id == "proj_nepse"
    assert "SYPNL" in result.system_prompt  # episodic memory retrieved
    assert "clarifying question" in result.system_prompt  # trait injected
    assert result.messages[-1]["content"] == "how is my NEPSE Portfolio doing?"
    assert any(m["content"] == "markets were wild today" for m in result.messages)


async def test_ambiguity_halts_over_real_stores(
    assembler: tuple[PromptAssembler, WorkingMemory, EntityResolver], user_id: str
) -> None:
    built, _, entities = assembler
    await entities.index(
        user_id, "project", "proj_a", "NEPSE Tracker",
        "tracking stock trades and investments on the Nepal exchange",
    )
    await entities.index(
        user_id, "project", "proj_b", "US Stocks Tracker",
        "tracking stock trades and investments on the US market",
    )

    result = await built.assemble(user_id, f"s_{uuid.uuid4().hex[:8]}", "my trading tracker?")

    assert isinstance(result, DisambiguationRequest)
    assert len(result.candidates) >= 2
