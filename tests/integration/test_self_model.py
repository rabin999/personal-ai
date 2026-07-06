"""Integration tests for the Self-Model (spec §9) — real Mongo + Qdrant (+ LLM for rewrite)."""

import uuid
from collections.abc import AsyncIterator

import pytest
from qdrant_client import models

from adapters.db import SELF_STATEMENTS_COLLECTION, USER_ID_FIELD, Database
from adapters.doc.mongo import MongoDocStore
from adapters.llm.openrouter import OpenRouterLLM
from adapters.vector.qdrant import QdrantVectorStore
from config.settings import Settings, get_settings
from core.reasoning.self_model import SelfModel, TurnRecord

pytestmark = pytest.mark.integration


@pytest.fixture
def user_id() -> str:
    return f"it_{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def self_model(db: Database) -> AsyncIterator[SelfModel]:
    settings = Settings(_env_file=None)
    await db.ensure_qdrant_collections()
    model = SelfModel(MongoDocStore(db), QdrantVectorStore(db, settings.embedding_model), llm=None)
    yield model
    await db.mongo("self_model_log").delete_many({"user_id": {"$regex": "^it_"}})
    await db.qdrant().delete(
        SELF_STATEMENTS_COLLECTION,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key=USER_ID_FIELD, match=models.MatchText(text="it_"))]
            )
        ),
    )


# Acceptance: every turn produces one self_model_log entry (persistence leg).
async def test_log_persists_turn_record_in_mongo(
    db: Database, self_model: SelfModel, user_id: str
) -> None:
    record = TurnRecord(user_id=user_id, confidence=0.7)
    await self_model.log(record, statement_text="try the lake trail this weekend")

    stored = await db.mongo("self_model_log").find_one({"_id": record.turn_id})
    assert stored is not None and stored["user_id"] == user_id


# Acceptance: recall surfaces a relevant prior statement for a repeated topic.
async def test_recall_surfaces_prior_statement_on_repeated_topic(
    self_model: SelfModel, user_id: str
) -> None:
    await self_model.log(
        TurnRecord(user_id=user_id),
        statement_text="I suggested splitting the NEPSE position into two smaller trades",
    )
    await self_model.log(
        TurnRecord(user_id=user_id),
        statement_text="the lake trail should be beautiful this weekend",
    )

    statements = await self_model.recall(user_id, "what did you tell me about my trades?")

    assert statements and "trades" in statements[0].text


async def test_recall_is_user_isolated(self_model: SelfModel, user_id: str) -> None:
    other = f"it_{uuid.uuid4().hex[:12]}"
    await self_model.log(
        TurnRecord(user_id=other), statement_text="secret advice about VORTEX99 holdings"
    )
    statements = await self_model.recall(user_id, "VORTEX99")
    assert all("VORTEX99" not in s.text for s in statements)


@pytest.mark.skipif(
    not get_settings().open_router_api_key,
    reason="OPEN_ROUTER_API_KEY not set — rewrite needs a real LLM",
)
async def test_real_llm_rewrites_overclaiming_draft(db: Database, user_id: str) -> None:
    settings = get_settings()
    model = SelfModel(
        MongoDocStore(db),
        QdrantVectorStore(db, settings.embedding_model),
        llm=OpenRouterLLM(settings),
    )

    check = await model.check_boundary(
        user_id, "I understand exactly how you feel — I feel your pain too."
    )

    assert check.flagged
    assert check.rewritten_text
    lowered = check.rewritten_text.lower()
    assert "exactly how you feel" not in lowered
    assert "i feel your pain" not in lowered
