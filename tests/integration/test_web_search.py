"""Integration tests for Web Search (spec §15).

Cache + ledger run against real Mongo and the summarizer against real
OpenRouter; the providers themselves are faked unless SERPER_API_KEY /
BRAVE_API_KEY are present (then one real query each verifies the adapters).
"""

import uuid
from collections.abc import AsyncIterator

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from adapters.llm.openrouter import OpenRouterLLM
from adapters.search.brave import BraveSearch
from adapters.search.serper import SerperSearch
from config.settings import get_settings
from core.cost import CostLedger
from core.tools.web_search import WebSearch
from ports.search import SearchResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not get_settings().open_router_api_key,
        reason="OPEN_ROUTER_API_KEY not set — §15 summarizer needs a real LLM",
    ),
]


class StaticProvider:
    name = "serper"
    cost_per_query_usd = 0.0003

    async def search(
        self, query: str, max_results: int = 8, *, recency: str | None = None
    ) -> list[SearchResult]:
        return [
            SearchResult(
                title="SYPNL phase-2 readout",
                url="https://example.com/sypnl",
                snippet="SYPNL's phase-2 trial met its primary endpoint; shares rose 12%.",
            )
        ]


@pytest.fixture
def user_id() -> str:
    return f"it_{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def stack(db: Database) -> AsyncIterator[tuple[WebSearch, CostLedger, Database]]:
    docs = MongoDocStore(db)
    ledger = CostLedger(docs)
    search = WebSearch(
        docs, OpenRouterLLM(get_settings(), ledger=ledger), StaticProvider(), ledger=ledger
    )
    yield search, ledger, db
    await db.mongo("search_cache").delete_many({})
    await db.mongo("cost_ledger").delete_many({"user_id": {"$regex": "^it_"}})


async def test_miss_then_cached_hit_with_real_stores_and_summarizer(
    stack: tuple[WebSearch, CostLedger, Database], user_id: str
) -> None:
    search, ledger, _ = stack
    query = f"SYPNL trial news {uuid.uuid4().hex[:6]}"

    first = await search.run(query, user_id, "s1")
    second = await search.run(query, user_id, "s1")
    await ledger.flush()

    assert not first.cache_hit and second.cache_hit
    assert first.summary and "sypnl" in first.summary.lower()
    assert second.summary == first.summary

    summary = await ledger.get(user_id, component="search")
    assert summary.count == 2
    assert summary.total_usd == pytest.approx(0.0003)  # miss cost + $0 hit


@pytest.mark.skipif(not get_settings().serper_api_key, reason="SERPER_API_KEY not set")
async def test_real_serper_adapter_returns_results() -> None:
    results = await SerperSearch(get_settings().serper_api_key).search("openrouter api")
    assert results and results[0].url


@pytest.mark.skipif(not get_settings().brave_api_key, reason="BRAVE_API_KEY not set")
async def test_real_brave_adapter_returns_results() -> None:
    results = await BraveSearch(get_settings().brave_api_key).search("openrouter api")
    assert results and results[0].url
