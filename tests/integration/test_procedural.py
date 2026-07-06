"""Integration tests for Procedural Memory (spec §7) against real MongoDB."""

import uuid
from collections.abc import AsyncIterator

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from core.memory.procedural import ProceduralMemory

pytestmark = pytest.mark.integration


@pytest.fixture
async def memory(db: Database) -> AsyncIterator[ProceduralMemory]:
    yield ProceduralMemory(MongoDocStore(db))
    await db.mongo("procedural").delete_many({"user_id": {"$regex": "^it_"}})


@pytest.fixture
def user_id() -> str:
    return f"it_{uuid.uuid4().hex[:12]}"


async def test_rule_lifecycle_promotion_and_demotion_in_mongo(
    memory: ProceduralMemory, user_id: str
) -> None:
    rule = await memory.add_candidate(
        user_id,
        rule_text="when user says they need a win, offer one concrete small task",
        trigger="need a win",
        action="offer a concrete task",
    )
    assert await memory.rules_for(user_id) == []

    for _ in range(5):
        await memory.reinforce(user_id, rule.id)
    promoted = await memory.rules_for(user_id)
    assert [r.id for r in promoted] == [rule.id]
    assert promoted[0].evidence_count == 6

    for _ in range(4):
        await memory.reinforce(user_id, rule.id, delta=-0.15)
    assert await memory.rules_for(user_id) == []


async def test_two_user_isolation_in_mongo(memory: ProceduralMemory, user_id: str) -> None:
    other = f"it_{uuid.uuid4().hex[:12]}"
    rule = await memory.add_candidate(
        user_id, rule_text="r", trigger="need a win", action="a"
    )
    for _ in range(5):
        await memory.reinforce(user_id, rule.id)

    assert await memory.rules_for(other) == []
    with pytest.raises(KeyError):
        await memory.reinforce(other, rule.id)
