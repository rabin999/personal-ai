"""Integration tests for the Cost Ledger (spec §3) against real MongoDB."""

import time
import uuid
from collections.abc import AsyncIterator

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from core.cost import CostEntry, CostLedger, CostMetadata

pytestmark = pytest.mark.integration


@pytest.fixture
def user_id() -> str:
    return f"it_{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def ledger(db: Database) -> AsyncIterator[CostLedger]:
    yield CostLedger(MongoDocStore(db))
    await db.mongo("cost_ledger").delete_many({"user_id": {"$regex": "^it_"}})


def _entry(user_id: str, **overrides: object) -> CostEntry:
    defaults: dict[str, object] = {
        "user_id": user_id,
        "component": "llm",
        "provider": "openrouter",
        "units": {"input_tokens": 1000, "output_tokens": 200},
        "cost_usd": 0.005,
    }
    return CostEntry.model_validate({**defaults, **overrides})


async def test_logged_entry_lands_in_mongo_with_full_schema(
    db: Database, ledger: CostLedger, user_id: str
) -> None:
    ledger.log(_entry(user_id))
    await ledger.flush()

    stored = await db.mongo("cost_ledger").find_one({"user_id": user_id})
    assert stored is not None
    assert stored["component"] == "llm"
    assert stored["units"] == {"input_tokens": 1000, "output_tokens": 200}
    assert stored["metadata"]["cache_hit"] is False


async def test_real_group_aggregation_totals_and_project_spend(
    ledger: CostLedger, user_id: str
) -> None:
    project = CostMetadata(project_id="proj_it")
    ledger.log(_entry(user_id, cost_usd=0.10, metadata=project))
    ledger.log(_entry(user_id, cost_usd=0.25, metadata=project, component="search"))
    ledger.log(_entry(user_id, cost_usd=0.05))  # no project
    await ledger.flush()

    summary = await ledger.get(user_id, breakdown_by="component")
    assert summary.total_usd == pytest.approx(0.40)
    assert summary.count == 3
    assert summary.breakdown is not None
    assert summary.breakdown["llm"] == pytest.approx(0.15)
    assert summary.breakdown["search"] == pytest.approx(0.25)

    assert await ledger.project_spend(user_id, "proj_it") == pytest.approx(0.35)


async def test_two_user_isolation_in_aggregates(ledger: CostLedger, user_id: str) -> None:
    other = f"it_{uuid.uuid4().hex[:12]}"
    ledger.log(_entry(user_id, cost_usd=0.10))
    ledger.log(_entry(other, cost_usd=9.99))
    await ledger.flush()

    assert (await ledger.get(user_id)).total_usd == pytest.approx(0.10)
    assert (await ledger.get(other)).total_usd == pytest.approx(9.99)


async def test_logging_adds_no_latency_to_the_calling_path(
    ledger: CostLedger, user_id: str
) -> None:
    # Acceptance: ledger writes leave p95 response latency unchanged. log()
    # only schedules a task, so even against real Mongo the calling path
    # must stay in the microsecond range.
    durations: list[float] = []
    for _ in range(100):
        started = time.perf_counter()
        ledger.log(_entry(user_id))
        durations.append(time.perf_counter() - started)
    await ledger.flush()

    p95 = sorted(durations)[94]
    assert p95 < 0.005
    assert (await ledger.get(user_id)).count == 100
