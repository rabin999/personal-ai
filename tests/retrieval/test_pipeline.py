"""End-to-end pipeline tests (query → VerifiedResult) on fixtures — deterministic.

Covers the four cardinalities, the §5 edge-case matrix, and MUTATION-PROVES the two
checks that matter (cross-corroboration, recency) through the whole adapter. No network,
no LLM — the live headline/latency proof is in test_real_call.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from adapters.retrieval import crawl4ai_adapter
from adapters.retrieval.config import RetrievalConfig
from adapters.retrieval.fetch import FetchedPage
from tests.retrieval.conftest import (
    FakeSearch,
    FixtureFetcher,
    ScriptedExtractor,
    build_pipeline,
    make_page,
    sr,
)

pytestmark = pytest.mark.asyncio

WORDS = "this page contains a real sentence of content well over the threshold count here"


def _search(*domains: str) -> FakeSearch:
    return FakeSearch([sr(d, f"https://{d}/article", f"snippet about {d}") for d in domains])


def _body(domain: str) -> str:
    # Genuinely distinct body per domain (unique tokens) — independent sites don't share
    # verbatim text. A near-identical body is syndication, deliberately collapsed by S6, so
    # the fixtures must NOT accidentally look syndicated.
    slug = domain.replace(".", "")
    return " ".join(f"{slug}word{k}" for k in range(20))


def _pages(*specs: tuple[str, str | None]) -> dict[str, FetchedPage]:
    pages: dict[str, FetchedPage] = {}
    for domain, date_iso in specs:
        url = f"https://{domain}/article"
        pages[url] = make_page(url, text=_body(domain), date_iso=date_iso)
    return pages


# ── CARDINALITY ───────────────────────────────────────────────────────────────
async def test_corroborated() -> None:
    search = _search("apnews.com", "reuters.com", "bbc.com")
    fetcher = FixtureFetcher(
        _pages(
            ("apnews.com", "2026-06-01"), ("reuters.com", "2026-06-02"), ("bbc.com", "2026-06-03")
        )
    )
    extractor = ScriptedExtractor(
        {
            "apnews.com": ("Sushila Karki", "text"),
            "reuters.com": ("Sushila Karki", "text"),
            "bbc.com": ("Sushila Karki", "text"),
        }
    )
    r = await build_pipeline(search=search, fetcher=fetcher, extractor=extractor).verify(
        "who is the prime minister of Nepal"
    )
    assert r.status == "corroborated"
    assert r.corroboration_count >= 2
    assert r.answer == "Sushila Karki"
    assert {s.domain for s in r.sources} >= {"apnews.com", "reuters.com"}
    assert r.formatted_voice
    # Adaptive: corroborated on the first wave of 2 → the 3rd source is never fetched.
    assert "https://bbc.com/article" not in fetcher.fetched


async def test_single_source() -> None:
    search = _search("apnews.com", "reuters.com")
    fetcher = FixtureFetcher(_pages(("apnews.com", "2026-06-01"), ("reuters.com", "2026-06-02")))
    extractor = ScriptedExtractor({"apnews.com": ("Sushila Karki", "text")})  # reuters: no answer
    r = await build_pipeline(search=search, fetcher=fetcher, extractor=extractor).verify(
        "obscure fact only one page has"
    )
    assert r.status == "single_source"
    assert r.corroboration_count == 0
    assert r.answer == "Sushila Karki"
    assert len(r.sources) == 1


async def test_conflicting_surfaces_both() -> None:
    search = _search("a.com", "b.com")
    fetcher = FixtureFetcher(_pages(("a.com", "2026-06-01"), ("b.com", "2026-06-02")))
    extractor = ScriptedExtractor({"a.com": ("Alice", "text"), "b.com": ("Bob", "text")})
    r = await build_pipeline(search=search, fetcher=fetcher, extractor=extractor).verify(
        "who won the disputed award"
    )
    assert r.status == "conflicting"
    assert r.answer is None
    assert r.conflict is not None
    assert {c.claim for c in r.conflict} == {"Alice", "Bob"}


async def test_not_found_is_honest_zero_never_fabricated() -> None:
    search = _search("a.com", "b.com")
    fetcher = FixtureFetcher(_pages(("a.com", None), ("b.com", None)))
    extractor = ScriptedExtractor({})  # nobody answers
    r = await build_pipeline(search=search, fetcher=fetcher, extractor=extractor).verify(
        "the made up flibbergibbet index of 2026"
    )
    assert r.status == "not_found"
    assert r.answer is None
    assert "couldn't find" in r.formatted_voice.lower()


# ── §5 EDGE CASES ─────────────────────────────────────────────────────────────
async def test_dead_source_does_not_fail_the_query() -> None:
    # apnews present; reuters URL missing from the fetcher → 404 per-source error, proceed.
    search = _search("apnews.com", "reuters.com")
    fetcher = FixtureFetcher(
        {
            "https://apnews.com/article": make_page(
                "https://apnews.com/article", text=WORDS, date_iso="2026-06-01"
            )
        }
    )
    extractor = ScriptedExtractor({"apnews.com": ("Sushila Karki", "text")})
    r = await build_pipeline(search=search, fetcher=fetcher, extractor=extractor).verify("q")
    assert r.status == "single_source"
    assert any("404" in e.reason for e in r.errors)


async def test_thin_content_is_rejected() -> None:
    search = _search("paywall.com", "apnews.com")
    fetcher = FixtureFetcher(
        {
            "https://paywall.com/article": make_page(
                "https://paywall.com/article", text="Subscribe now"
            ),
            "https://apnews.com/article": make_page(
                "https://apnews.com/article", text=WORDS, date_iso="2026-06-01"
            ),
        }
    )
    extractor = ScriptedExtractor(
        {"paywall.com": ("teaser", "text"), "apnews.com": ("real", "text")}
    )
    cfg = RetrievalConfig(word_count_threshold=5)
    pipe = build_pipeline(search=search, fetcher=fetcher, extractor=extractor, config=cfg)
    r = await pipe.verify("q")
    # The thin paywall teaser is rejected; only the real page survives.
    assert any("thin" in e.reason for e in r.errors)
    assert {s.domain for s in r.sources} == {"apnews.com"}


async def test_redirect_uses_final_url() -> None:
    search = _search("old.com")
    final = "https://new.com/final"
    page = make_page(final, text=WORDS, date_iso="2026-06-01")
    page.requested_url = "https://old.com/article"  # server followed a redirect
    fetcher = FixtureFetcher({"https://old.com/article": page})
    extractor = ScriptedExtractor({"new.com": ("answer", "text")})
    r = await build_pipeline(search=search, fetcher=fetcher, extractor=extractor).verify("q")
    assert r.sources[0].url == final
    assert r.sources[0].domain == "new.com"


async def test_all_sources_fail_returns_error_never_fabricates() -> None:
    search = _search("a.com", "b.com")
    fetcher = FixtureFetcher({})  # every fetch 404s
    extractor = ScriptedExtractor({"a.com": ("x", "text")})
    r = await build_pipeline(search=search, fetcher=fetcher, extractor=extractor).verify("q")
    assert r.status == "error"
    assert r.answer is None
    assert len(r.errors) >= 2


async def test_search_provider_down_degrades_honestly() -> None:
    search = FakeSearch([], fail=True)
    pipe = build_pipeline(
        search=search, fetcher=FixtureFetcher({}), extractor=ScriptedExtractor({})
    )
    r = await pipe.verify("q")
    assert r.status == "error"
    assert r.formatted_voice  # honest line, not a crash
    assert any("search" in e.reason for e in r.errors)


async def test_crawler_down_degrades_to_snippet_only() -> None:
    search = _search("apnews.com", "reuters.com")
    fetcher = FixtureFetcher({}, server_down=True)
    extractor = ScriptedExtractor(
        {"apnews.com": ("Sushila Karki", "text"), "reuters.com": ("Sushila Karki", "text")}
    )
    r = await build_pipeline(search=search, fetcher=fetcher, extractor=extractor).verify("q")
    # It still produced a grounded answer from snippets, but flagged the degrade + low conf.
    assert any("crawler down" in e.reason for e in r.errors)
    assert r.confidence <= 0.3
    assert r.answer == "Sushila Karki"


async def test_want_json_populates_structured_output() -> None:
    search = _search("a.com", "b.com")
    fetcher = FixtureFetcher(_pages(("a.com", "2026-06-01"), ("b.com", "2026-06-02")))
    extractor = ScriptedExtractor({"a.com": ("42", "number"), "b.com": ("42", "number")})
    r = await build_pipeline(search=search, fetcher=fetcher, extractor=extractor).verify(
        "the number", want_json=True
    )
    assert r.formatted_json is not None
    assert r.formatted_json["answer"] == "42"


# ── RECENCY (as important as content) ─────────────────────────────────────────
async def test_corroborated_but_stale_is_flagged_not_dropped() -> None:
    # Both agree but both are old on a time-sensitive query → keep, flag is_stale.
    search = _search("a.com", "b.com")
    fetcher = FixtureFetcher(_pages(("a.com", "2018-01-01"), ("b.com", "2018-02-01")))
    extractor = ScriptedExtractor(
        {"a.com": ("Old Answer", "text"), "b.com": ("Old Answer", "text")}
    )
    r = await build_pipeline(search=search, fetcher=fetcher, extractor=extractor).verify(
        "current officeholder", time_sensitive=True
    )
    assert r.status == "corroborated"
    assert r.recency.is_stale is True


async def test_recency_drops_stale_when_fresh_corroboration_exists() -> None:
    # 1 FRESH source says A; 2 STALE sources say B (wrong/old). Recency must keep A, drop B.
    search = _search("fresh.com", "stale1.com", "stale2.com")
    fetcher = FixtureFetcher(
        _pages(
            ("fresh.com", "2026-06-01"), ("stale1.com", "2017-01-01"), ("stale2.com", "2017-02-01")
        )
    )
    extractor = ScriptedExtractor(
        {
            "fresh.com": ("New PM", "text"),
            "stale1.com": ("Old PM", "text"),
            "stale2.com": ("Old PM", "text"),
        }
    )
    cfg = RetrievalConfig(word_count_threshold=3, initial_sources=3)  # fetch all in one wave
    pipe = build_pipeline(search=search, fetcher=fetcher, extractor=extractor, config=cfg)
    r = await pipe.verify("current prime minister", time_sensitive=True)
    assert r.answer == "New PM"  # the stale majority did NOT win
    assert "Old PM" not in {s.snippet for s in r.sources}


async def test_mutation_recency_filter_is_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break recency (is_stale → always False) and the STALE answer wins. Proves the
    recency filter is what stops a stale page passing on a time-sensitive query."""
    search = _search("fresh.com", "stale1.com", "stale2.com")
    fetcher = FixtureFetcher(
        _pages(
            ("fresh.com", "2026-06-01"), ("stale1.com", "2017-01-01"), ("stale2.com", "2017-02-01")
        )
    )
    extractor = ScriptedExtractor(
        {
            "fresh.com": ("New PM", "text"),
            "stale1.com": ("Old PM", "text"),
            "stale2.com": ("Old PM", "text"),
        }
    )
    cfg = RetrievalConfig(word_count_threshold=3, initial_sources=3)
    monkeypatch.setattr(crawl4ai_adapter, "is_stale", lambda *a, **k: False)  # BREAK recency
    pipe = build_pipeline(search=search, fetcher=fetcher, extractor=extractor, config=cfg)
    r = await pipe.verify("current prime minister", time_sensitive=True)
    assert r.answer == "Old PM"  # stale majority now wrongly wins → the filter mattered


# ── OPERATIONAL: timings + fast-path budget ──────────────────────────────────
async def test_timings_are_recorded() -> None:
    search = _search("a.com", "b.com")
    fetcher = FixtureFetcher(_pages(("a.com", "2026-06-01"), ("b.com", "2026-06-02")))
    extractor = ScriptedExtractor({"a.com": ("X", "text"), "b.com": ("X", "text")})
    r = await build_pipeline(search=search, fetcher=fetcher, extractor=extractor).verify("q")
    assert r.timings.total_ms >= 0.0
    assert r.timings.search_ms >= 0.0


async def test_budget_stops_widening_and_marks_partial() -> None:
    # Non-corroborating first wave WOULD widen to a 3rd source, but a 0ms budget stops it.
    search = _search("a.com", "b.com", "c.com")
    fetcher = FixtureFetcher(_pages(("a.com", None), ("b.com", None), ("c.com", None)))
    extractor = ScriptedExtractor(
        {"a.com": ("Alice", "text"), "b.com": ("Bob", "text"), "c.com": ("Alice", "text")}
    )
    r = await build_pipeline(search=search, fetcher=fetcher, extractor=extractor).verify(
        "q", budget_ms=0
    )
    assert "https://c.com/article" not in fetcher.fetched  # widening was gated by the budget
    assert any("partial" in e.reason for e in r.errors)


# ── TRACING: every stage emits a span with a brief description ────────────────
class _CapturingLog:
    """Stands in for the project's StructuredLogger — captures the span records."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def log(self, level: str, event: str, **fields: Any) -> None:
        self.records.append({"level": level, "event": event, **fields})


async def test_every_stage_emits_a_trace_span() -> None:
    from adapters.retrieval.crawl4ai_adapter import Crawl4AIRetrieval
    from adapters.retrieval.trace import RetrievalTracer
    from tests.retrieval.conftest import DeterministicFormatter

    log = _CapturingLog()
    tracer = RetrievalTracer(logs=log, user_id="u1", session_id="s1")
    search = _search("apnews.com", "reuters.com")
    fetcher = FixtureFetcher(_pages(("apnews.com", "2026-06-01"), ("reuters.com", "2026-06-02")))
    extractor = ScriptedExtractor(
        {"apnews.com": ("Sushila Karki", "text"), "reuters.com": ("Sushila Karki", "text")}
    )
    pipe = Crawl4AIRetrieval(
        search=search,
        fetcher=fetcher,
        extractor=extractor,
        formatter=DeterministicFormatter(),
        config=RetrievalConfig(word_count_threshold=3),
        tracer=tracer,
    )
    await pipe.verify("who is the prime minister of Nepal")

    events = {r["event"] for r in log.records}
    # Each pipeline stage left a span, and every span carries a brief description.
    assert {
        "retrieval.search",
        "retrieval.select",
        "retrieval.fetch",
        "retrieval.cross_check",
        "retrieval.format",
        "retrieval.done",
    } <= events
    assert all("description" in r for r in log.records)
    fetch_span = next(r for r in log.records if r["event"] == "retrieval.fetch")
    assert "duration_ms" in fetch_span  # timing recorded per stage
