"""Unit tests for the Cost Ledger (spec §3) — DocStore faked in memory."""

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from core.cost import COST_COLLECTION, CostEntry, CostLedger, CostMetadata
from tests.fakes import FakeDocStore


def _entry(user_id: str = "u_demo_001", **overrides: Any) -> CostEntry:
    defaults: dict[str, Any] = {
        "user_id": user_id,
        "component": "llm",
        "provider": "openrouter",
        "units": {"input_tokens": 900, "output_tokens": 150},
        "cost_usd": 0.0042,
    }
    return CostEntry(**{**defaults, **overrides})


@pytest.fixture
def docs() -> FakeDocStore:
    return FakeDocStore()


@pytest.fixture
def ledger(docs: FakeDocStore) -> CostLedger:
    return CostLedger(docs)


# ── acceptance 1: one call → exactly one entry, correct units + cost ─────


async def test_one_call_logs_exactly_one_entry_with_units_and_cost(
    docs: FakeDocStore, ledger: CostLedger
) -> None:
    ledger.log(_entry())
    await ledger.flush()

    rows = await docs.find(COST_COLLECTION)
    assert len(rows) == 1
    assert rows[0]["units"] == {"input_tokens": 900, "output_tokens": 150}
    assert rows[0]["cost_usd"] == 0.0042
    assert rows[0]["user_id"] == "u_demo_001"
    assert rows[0]["timestamp"]  # ISO timestamp present


# ── acceptance 2: cache hit → cost 0, cache_hit true ─────────────────────


async def test_cache_hit_logs_zero_cost_with_flag(
    docs: FakeDocStore, ledger: CostLedger
) -> None:
    ledger.log(_entry(cost_usd=0.0, metadata=CostMetadata(cache_hit=True)))
    await ledger.flush()

    (row,) = await docs.find(COST_COLLECTION)
    assert row["cost_usd"] == 0.0
    assert row["metadata"]["cache_hit"] is True


def test_negative_cost_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _entry(cost_usd=-0.01)


# ── rule 4: logging never delays the response ────────────────────────────


async def test_log_returns_immediately_even_when_the_write_is_slow(
    ledger: CostLedger, docs: FakeDocStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_insert = docs.insert

    async def slow_insert(collection: str, doc: Mapping[str, Any]) -> str:
        await asyncio.sleep(0.2)
        return await real_insert(collection, doc)

    monkeypatch.setattr(docs, "insert", slow_insert)

    started = time.perf_counter()
    ledger.log(_entry())
    elapsed = time.perf_counter() - started

    assert elapsed < 0.05  # scheduled, not awaited
    await ledger.flush()
    assert len(await docs.find(COST_COLLECTION)) == 1


async def test_failed_write_is_swallowed_not_raised(
    ledger: CostLedger, docs: FakeDocStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_insert(collection: str, doc: Mapping[str, Any]) -> str:
        raise ConnectionError("mongo down")

    monkeypatch.setattr(docs, "insert", broken_insert)
    ledger.log(_entry())
    await ledger.flush()  # must not raise


# ── aggregation ──────────────────────────────────────────────────────────


async def test_get_totals_and_breakdown_by_component(ledger: CostLedger) -> None:
    ledger.log(_entry(cost_usd=0.01))
    ledger.log(_entry(cost_usd=0.02, component="tts", units={"characters": 400}))
    await ledger.flush()

    summary = await ledger.get("u_demo_001", breakdown_by="component")

    assert summary.count == 2
    assert summary.total_usd == pytest.approx(0.03)
    assert summary.breakdown is not None
    assert summary.breakdown["llm"] == pytest.approx(0.01)
    assert summary.breakdown["tts"] == pytest.approx(0.02)


async def test_get_is_user_scoped(ledger: CostLedger) -> None:
    ledger.log(_entry("u_demo_001", cost_usd=0.01))
    ledger.log(_entry("u_demo_002", cost_usd=5.00))
    await ledger.flush()

    summary = await ledger.get("u_demo_001")

    assert summary.total_usd == pytest.approx(0.01)
    assert summary.count == 1


# ── acceptance 3: project_spend over a date range ────────────────────────


async def test_project_spend_sums_one_project_within_range(ledger: CostLedger) -> None:
    project = CostMetadata(project_id="proj_stocks")
    ledger.log(
        _entry(cost_usd=0.10, metadata=project, timestamp="2026-07-01T10:00:00+00:00")
    )
    ledger.log(
        _entry(cost_usd=0.20, metadata=project, timestamp="2026-07-03T10:00:00+00:00")
    )
    # Outside the range / different project / different user — all excluded.
    ledger.log(
        _entry(cost_usd=9.0, metadata=project, timestamp="2026-06-01T10:00:00+00:00")
    )
    ledger.log(_entry(cost_usd=9.0, metadata=CostMetadata(project_id="proj_other")))
    ledger.log(_entry("u_demo_002", cost_usd=9.0, metadata=project))
    await ledger.flush()

    spend = await ledger.project_spend(
        "u_demo_001",
        "proj_stocks",
        since="2026-07-01T00:00:00+00:00",
        until="2026-07-04T00:00:00+00:00",
    )

    assert spend == pytest.approx(0.30)
