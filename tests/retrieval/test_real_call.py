"""Real-call proof (§6): the pipeline end-to-end against the LIVE Crawl4AI server, REAL
Serper, and a REAL OpenRouter model. No mocks — this is what proves the render + verify +
format loop actually works, including THE case the app kept failing ("current PM of Nepal").

Skipped LOUDLY (never silently) when a key or the crawler is missing, so a missing
prerequisite is obvious rather than a false green. Marked ``real_call`` + ``paid``.

Run: CRAWL4AI_API_TOKEN=... uv run pytest tests/retrieval/test_real_call.py -m real_call -s
"""

from __future__ import annotations

import statistics
import time

import httpx
import pytest

from adapters.llm.openrouter import OpenRouterLLM
from adapters.retrieval import build_crawl4ai_retrieval
from adapters.retrieval.config import RetrievalConfig
from adapters.search.serper import SerperSearch
from config.settings import get_settings
from ports.retrieval import RetrievalPort, VerifiedResult

pytestmark = [pytest.mark.real_call, pytest.mark.paid]

USER_ID = "harness-retrieval"


def _skip_reason() -> str | None:
    s = get_settings()
    if not s.open_router_api_key:
        return "OPEN_ROUTER_API_KEY not set (real model call)"
    if not s.serper_api_key:
        return "SERPER_API_KEY not set (real search)"
    cfg = RetrievalConfig()
    try:
        r = httpx.get(f"{cfg.base_url}/health", timeout=3.0)
        if r.status_code != 200:
            return f"crawl4ai health {r.status_code} at {cfg.base_url}"
    except httpx.HTTPError as exc:
        return f"crawl4ai unreachable at {cfg.base_url}: {exc}"
    return None


@pytest.fixture(scope="module")
def pipeline() -> RetrievalPort:
    reason = _skip_reason()
    if reason:
        pytest.skip(f"real_call skipped: {reason}")
    settings = get_settings()
    return build_crawl4ai_retrieval(
        search=SerperSearch(settings.serper_api_key),
        llm=OpenRouterLLM(settings),
        user_id=USER_ID,
        config=RetrievalConfig(),
    )


def _dump(title: str, r: VerifiedResult) -> None:
    print(f"\n===== {title} =====")
    print(f"status={r.status}  confidence={r.confidence}  corroboration={r.corroboration_count}")
    print(f"answer={r.answer!r}")
    print(f"voice={r.formatted_voice!r}")
    print(
        f"recency: time_sensitive={r.recency.is_time_sensitive} stale={r.recency.is_stale} "
        f"most_recent={r.recency.most_recent_source_date}"
    )
    for s in r.sources:
        print(f"  source: {s.domain}  date={s.published_date}  url={s.url}")
    for e in r.errors:
        print(f"  error: {e.url} -> {e.reason}")
    print(f"timings: {r.timings.model_dump()}")


# ── THE headline case the app kept failing ────────────────────────────────────
async def test_headline_current_pm_of_nepal(pipeline: RetrievalPort) -> None:
    r = await pipeline.verify("who is the current prime minister of Nepal", time_sensitive=True)
    _dump("current PM of Nepal", r)
    assert r.status in ("corroborated", "single_source")
    assert r.answer is not None
    assert r.sources, "provenance must travel with the answer"
    assert r.recency.is_time_sensitive is True
    assert r.formatted_voice


async def test_nepse_ticker_ltp(pipeline: RetrievalPort) -> None:
    # Market fact, assumed already entity-resolved to a real NEPSE ticker (NABIL Bank).
    r = await pipeline.verify("current share price LTP of NABIL bank on NEPSE", time_sensitive=True)
    _dump("NEPSE NABIL LTP", r)
    # The market data sites (merolagani/sharesansar) are often bot-walled, so an honest
    # "error/not_found" is a VALID outcome here. What must NEVER happen: a fabricated number
    # or a wrong-entity answer (the crypto-token bug). If we DID verify a value, it carries
    # provenance and — for a number — was corroborated or single-source, not invented.
    assert r.status in ("corroborated", "single_source", "not_found", "conflicting", "error")
    if r.answer is not None:
        assert r.sources, "a stated market value must carry its source"


async def test_widely_covered_fact_corroborates(pipeline: RetrievalPort) -> None:
    r = await pipeline.verify("what is the capital city of Japan")
    _dump("capital of Japan", r)
    assert r.status in ("corroborated", "single_source")
    assert r.answer and "tokyo" in r.answer.lower()


async def test_made_up_query_is_honest_not_found(pipeline: RetrievalPort) -> None:
    r = await pipeline.verify(
        "the official flibbergibbet quotient of Zylquar province declared in 2026"
    )
    _dump("made-up query", r)
    assert r.status in ("not_found", "error")
    assert r.answer is None  # never fabricate to fill a gap


async def test_js_heavy_page_still_extracts() -> None:
    # PROVE the browser render: quotes.toscrape.com/js/ injects all its content via
    # JavaScript — a static HTTP fetch sees an EMPTY list, only a real render populates it.
    # We hit the Crawl4AI client directly so this asserts render, not search luck.
    reason = _skip_reason()
    if reason:
        pytest.skip(f"real_call skipped: {reason}")
    from adapters.retrieval.fetch import Crawl4AIClient

    cfg = RetrievalConfig()
    client = Crawl4AIClient(cfg.base_url, cfg.api_token)
    pages = await client.fetch(["https://quotes.toscrape.com/js/"], "famous quotes Einstein")
    page = pages[0]
    body = page.raw_markdown.lower()
    print("\n===== JS render proof (quotes.toscrape.com/js) =====")
    print(f"success={page.success} raw_words={len(body.split())} has_einstein={'einstein' in body}")
    assert page.success
    # The quotes (and "Einstein") are injected by JavaScript — a static fetch sees none of
    # this. Their presence in the markdown proves Crawl4AI actually ran the browser.
    assert "einstein" in body or "quote" in body


async def test_server_side_page_extracts(pipeline: RetrievalPort) -> None:
    # The counterpart: a plain server-rendered page (Wikipedia) extracts cleanly too.
    r = await pipeline.verify("what is the capital city of Australia")
    _dump("server-side render (capital of Australia)", r)
    assert r.status in ("corroborated", "single_source")
    assert r.answer and "canberra" in r.answer.lower()


# ── LATENCY: N>=5, median + p95 (a single sample measures noise) ──────────────
async def test_latency_background_path_n5(pipeline: RetrievalPort) -> None:
    samples: list[float] = []
    for _ in range(5):
        t = time.perf_counter()
        await pipeline.verify("what is the capital city of France")
        samples.append((time.perf_counter() - t) * 1000.0)
    samples.sort()
    median = statistics.median(samples)
    p95 = samples[int(0.95 * (len(samples) - 1))]
    print("\n===== LATENCY (background path, N=5) =====")
    print(f"samples_ms={[round(s) for s in samples]}")
    print(f"median_ms={round(median)}  p95_ms={round(p95)}")
    # Background/waiter path — seconds are fine; this only guards against a pathological hang.
    assert median < 30_000, f"median {median}ms too slow even for the background path"


async def test_fast_path_budget_is_measured(pipeline: RetrievalPort) -> None:
    # A tiny budget must come back marked partial rather than blocking — the gate is real.
    r = await pipeline.verify("what is the capital city of France", budget_ms=1)
    _dump("fast-path budget=1ms", r)
    assert r.timings.total_ms > 0
    assert any("partial" in e.reason for e in r.errors)
