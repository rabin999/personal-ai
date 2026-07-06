"""Integration tests for the Psychological User-Model (spec §17) — real Mongo."""

import uuid
from collections.abc import AsyncIterator

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from core.psych.user_model import PsychUserModel

pytestmark = pytest.mark.integration


@pytest.fixture
async def psych(db: Database) -> AsyncIterator[PsychUserModel]:
    yield PsychUserModel(MongoDocStore(db))
    await db.mongo("psych_model").delete_many({"_id": {"$regex": "^it_"}})


@pytest.fixture
def user_id() -> str:
    return f"it_{uuid.uuid4().hex[:12]}"


async def test_model_persists_and_rehydrates(psych: PsychUserModel, user_id: str) -> None:
    for _ in range(5):
        await psych.update_trait(user_id, "agreeableness", 0.8)
        await psych.update_mood(user_id, valence=0.3, arousal=-0.1)
    await psych.set_stage(user_id, "social_withdrawal", "contemplation")

    fresh = PsychUserModel(psych._docs)  # new instance, same store
    model = await fresh.get(user_id)

    assert model.ocean["agreeableness"].confidence > 0.2
    assert model.mood_baseline.samples == 5
    assert model.stages["social_withdrawal"] == "contemplation"


async def test_two_user_isolation(psych: PsychUserModel, user_id: str) -> None:
    other = f"it_{uuid.uuid4().hex[:12]}"
    for _ in range(5):
        await psych.update_trait(user_id, "openness", 0.9)

    assert (await psych.get(other)).ocean["openness"].confidence == 0.0
