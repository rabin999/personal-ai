"""Unit tests for Web Search (spec §15) — providers, LLM, and store faked."""

import pytest

from core.cost import COST_COLLECTION, CostLedger
from core.tools.web_search import SEARCH_CACHE_COLLECTION, WebSearch, _ttl_for
from ports.search import SearchProviderError, SearchResult
from tests.fakes import FakeDocStore, FakeLLM

RESULTS = [
    SearchResult(title="SYPNL up 12%", url="https://x/1", snippet="trial met endpoint"),
    SearchResult(title="Biotech rally", url="https://x/2", snippet="sector wide gains"),
]


class FakeProvider:
    def __init__(self, name: str, results: list[SearchResult] | None = None, fail: bool = False):
        self.name = name
        self.cost_per_query_usd = 0.0003 if name == "serper" else 0.003
        self.results = results or RESULTS
        self.fail = fail
        self.calls = 0

    async def search(
        self, query: str, max_results: int = 8, *, recency: str | None = None
    ) -> list[SearchResult]:
        self.calls += 1
        self.last_recency = recency
        if self.fail:
            raise SearchProviderError(f"{self.name} down")
        return self.results


def _stack(
    primary_fail: bool = False,
) -> tuple[WebSearch, FakeDocStore, CostLedger, FakeProvider, FakeProvider, FakeLLM]:
    docs = FakeDocStore()
    ledger = CostLedger(docs)
    serper = FakeProvider("serper", fail=primary_fail)
    brave = FakeProvider("brave")
    llm = FakeLLM(["SYPNL's trial met its endpoint and the stock rose about 12%."])
    search = WebSearch(docs, llm, serper, brave, ledger)
    return search, docs, ledger, serper, brave, llm


# Acceptance: a miss hits Serper, summarizes, logs real cost.
async def test_miss_hits_serper_summarizes_and_logs_cost() -> None:
    search, docs, ledger, serper, brave, llm = _stack()

    outcome = await search.run("SYPNL news today", "u_demo_001", "s1")
    await ledger.flush()

    assert serper.calls == 1 and brave.calls == 0
    assert not outcome.cache_hit
    assert outcome.summary.startswith("SYPNL's trial met")
    assert len(outcome.sources) == 2
    (row,) = await docs.find(COST_COLLECTION)
    assert row["component"] == "search" and row["provider"] == "serper"
    assert row["cost_usd"] == pytest.approx(0.0003)
    assert row["metadata"]["cache_hit"] is False
    assert llm.calls[0]["tier"] == "simple"  # summarizer is the cheap tier


# An empty/blank query is a no-op: no provider call, no cost, no cache write
# (providers 400 on an empty query — a queued task with no query must not spend).
async def test_blank_query_is_a_noop() -> None:
    search, docs, ledger, serper, brave, _ = _stack()

    for blank in ("", "   ", "\n"):
        outcome = await search.run(blank, "u_demo_001", "s1")
        assert outcome.sources == [] and outcome.provider == "none"
    await ledger.flush()

    assert serper.calls == 0 and brave.calls == 0
    assert await docs.find(COST_COLLECTION) == []
    assert await docs.find(SEARCH_CACHE_COLLECTION) == []


# Acceptance: repeat within TTL returns cache with a $0 ledger entry.
async def test_repeat_query_within_ttl_serves_cache_at_zero_cost() -> None:
    search, docs, ledger, serper, _, _ = _stack()
    # A STABLE lookup (no "now/latest/news" words) → cacheable. Breaking-news queries
    # deliberately bypass the cache (see the recency test), so they can't test cache hits.
    await search.run("SYPNL company overview", "u_demo_001", "s1")

    outcome = await search.run("SYPNL company overview", "u_demo_001", "s1")
    await ledger.flush()

    assert serper.calls == 1  # no second provider hit
    assert outcome.cache_hit
    rows = await docs.find(COST_COLLECTION)
    hits = [r for r in rows if r["metadata"]["cache_hit"]]
    assert len(hits) == 1 and hits[0]["cost_usd"] == 0.0


async def test_expired_cache_entry_triggers_fresh_search() -> None:
    search, docs, _ledger, serper, _, llm = _stack()
    llm.responses.append("fresh summary")
    await search.run("SYPNL company overview", "u_demo_001", "s1")  # stable → cacheable

    # Age the cache entry past its TTL.
    (key,) = docs.collections[SEARCH_CACHE_COLLECTION].keys()
    entry = docs.collections[SEARCH_CACHE_COLLECTION][key]
    entry["cached_at"] = "2020-01-01T00:00:00+00:00"

    outcome = await search.run("SYPNL company overview", "u_demo_001", "s1")
    assert serper.calls == 2
    assert not outcome.cache_hit


# Acceptance: simulated Serper outage falls back to Brave.
async def test_serper_outage_falls_back_to_brave() -> None:
    search, docs, ledger, serper, brave, _ = _stack(primary_fail=True)

    outcome = await search.run("SYPNL news", "u_demo_001", "s1")
    await ledger.flush()

    assert serper.calls == 1 and brave.calls == 1
    assert outcome.provider == "brave"
    (row,) = await docs.find(COST_COLLECTION)
    assert row["provider"] == "brave" and row["cost_usd"] == pytest.approx(0.003)


async def test_both_providers_down_reports_gracefully() -> None:
    """Both providers down → never raise/error to the user; return a plain 'couldn't
    look that up' summary the companion can convey (user request: inform, don't block)."""
    docs = FakeDocStore()
    search = WebSearch(
        docs, FakeLLM(), FakeProvider("serper", fail=True), FakeProvider("brave", fail=True)
    )
    outcome = await search.run("anything", "u_demo_001")
    assert outcome.sources == [] and outcome.provider == "none"
    assert "didn't go through" in outcome.summary or "couldn't" in outcome.summary.lower()


async def test_recency_biases_current_queries_but_not_historical() -> None:
    docs = FakeDocStore()
    provider = FakeProvider("serper")
    search = WebSearch(docs, FakeLLM(["ok", "ok", "ok", "ok"]), provider)
    # "latest / right now" → the tightest window (past DAY): recent means CURRENT.
    await search.run("latest on the missing plane right now", "u_demo_001")
    assert provider.last_recency == "day"
    # An unfolding event without now-words → past WEEK.
    await search.run("the plane that went missing near Pakistan", "u_demo_001")
    assert provider.last_recency == "week"
    # A neutral / evergreen lookup → NO date window (a forced window returns zero
    # results for facts that don't change; relevance already favours fresh pages).
    await search.run("good restaurants in Lisbon", "u_demo_001")
    assert provider.last_recency is None
    # An explicit historical year → no freshness filter at all.
    await search.run("who painted the Mona Lisa in 1503", "u_demo_001")
    assert provider.last_recency is None


async def test_summarizer_failure_falls_back_to_snippets() -> None:
    from ports.llm import LLMUnavailable

    docs = FakeDocStore()
    llm = FakeLLM([LLMUnavailable("down"), LLMUnavailable("down")])
    search = WebSearch(docs, llm, FakeProvider("serper"))
    outcome = await search.run("SYPNL", "u_demo_001")
    assert "trial met endpoint" in outcome.summary


def test_time_sensitive_queries_get_short_ttl() -> None:
    assert _ttl_for("is the market open today") == 15 * 60
    assert _ttl_for("capital of nepal") == 24 * 3600


async def test_task_handler_runs_search_off_the_conversation_path() -> None:
    search, _, _, _, _, _ = _stack()
    handler = search.as_task_handler()
    from ports.queue import QueuedTask

    task = QueuedTask(
        task_id="t1",
        session_id="s1",
        user_id="u_demo_001",
        type="web_search",
        params={"query": "SYPNL news"},
        created_at="2026-07-06T10:00:00+00:00",
    )
    result = await handler(task)
    assert "summary" in result and result["provider"] == "serper"
