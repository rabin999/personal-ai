"""Standalone harness fixtures for the verified-retrieval adapter.

The pipeline is driven query-in → VerifiedResult-out through its OWN ports (search,
page-fetcher, extractor, formatter) — NOT through the full engine. Fixture fetchers and
a scripted extractor make the cardinality/edge/mutation tests deterministic; the
``real_call`` suite (test_real_call.py) swaps in the live Crawl4AI server + real Serper +
real LLM. A fixture that CAN'T fail is worthless, so the fakes carry real page HTML/dates
and the mapping goes through the SAME ``page_from_crawl_result`` the live client uses.
"""

from __future__ import annotations

from typing import Any

from adapters.retrieval.config import RetrievalConfig
from adapters.retrieval.crawl4ai_adapter import Crawl4AIRetrieval
from adapters.retrieval.extract import ExtractedClaim
from adapters.retrieval.fetch import FetchedPage, page_from_crawl_result
from adapters.retrieval.format import deterministic_voice
from ports.retrieval import VerifiedResult
from ports.search import SearchProviderError, SearchResult


# ── fakes ─────────────────────────────────────────────────────────────────────
class FakeSearch:
    name = "fake"
    cost_per_query_usd = 0.0

    def __init__(self, results: list[SearchResult], *, fail: bool = False) -> None:
        self._results = results
        self._fail = fail
        self.calls: list[str] = []

    async def search(
        self, query: str, max_results: int = 8, *, recency: str | None = None
    ) -> list[SearchResult]:
        self.calls.append(query)
        if self._fail:
            raise SearchProviderError("fake search down")
        return self._results[:max_results]


class FixtureFetcher:
    """Maps requested URL → a fixture page. Unknown URLs yield a clean per-source error.
    ``server_down=True`` simulates the whole Crawl4AI service being unreachable."""

    def __init__(self, pages: dict[str, FetchedPage], *, server_down: bool = False) -> None:
        self._pages = pages
        self._server_down = server_down
        self.fetched: list[str] = []

    async def fetch(self, urls: list[str], query: str) -> list[FetchedPage]:
        self.fetched.extend(urls)
        if self._server_down:
            return [
                FetchedPage(
                    requested_url=u,
                    success=False,
                    server_down=True,
                    error_reason="crawl4ai unreachable",
                )
                for u in urls
            ]
        out: list[FetchedPage] = []
        for u in urls:
            page = self._pages.get(u)
            if page is None:
                out.append(
                    FetchedPage(requested_url=u, success=False, error_reason="404 not found")
                )
            else:
                out.append(page)
        return out


class ScriptedExtractor:
    """Domain → the answer that domain's page gives (or None = topic without answer).

    Keeps cross-check inputs fully deterministic so cardinality and mutation tests are
    exact — no LLM, no network."""

    def __init__(self, by_domain: dict[str, tuple[str, str]]) -> None:
        # by_domain: domain -> (answer, kind)
        self._by_domain = by_domain

    async def extract(self, query: str, page: FetchedPage) -> ExtractedClaim | None:
        entry = self._by_domain.get(page.domain)
        if entry is None:
            return None
        answer, kind = entry
        return ExtractedClaim(
            domain=page.domain,
            url=page.final_url,
            answer=answer,
            kind=kind,
            text=page.best_text()[:400],
        )


class DeterministicFormatter:
    async def format(
        self, query: str, result: VerifiedResult, *, want_json: bool
    ) -> tuple[str, dict[str, Any] | None]:
        voice = deterministic_voice(query, result)
        js: dict[str, Any] = {
            "answer": result.answer,
            "sources": [s.domain for s in result.sources],
        }
        return voice, (js if want_json else None)


# ── builders ────────────────────────────────────────────────────────────────
def make_page(
    url: str, *, text: str, date_iso: str | None = None, success: bool = True
) -> FetchedPage:
    """A fixture page built through the real CrawlResult→FetchedPage mapping."""
    html = ""
    if date_iso:
        html = f'<meta property="article:published_time" content="{date_iso}T00:00:00Z">'
    obj = {
        "url": url,
        "success": success,
        "status_code": 200 if success else 500,
        "markdown": {"raw_markdown": text, "fit_markdown": text},
        "html": html,
        "metadata": {"title": "fixture"},
        "error_message": "" if success else "server error",
    }
    return page_from_crawl_result(obj)


def sr(title: str, url: str, snippet: str) -> SearchResult:
    return SearchResult(title=title, url=url, snippet=snippet)


def build_pipeline(
    *,
    search: FakeSearch,
    fetcher: FixtureFetcher,
    extractor: ScriptedExtractor,
    config: RetrievalConfig | None = None,
) -> Crawl4AIRetrieval:
    cfg = config or RetrievalConfig(word_count_threshold=3, stale_after_days=120)
    return Crawl4AIRetrieval(
        search=search,
        fetcher=fetcher,
        extractor=extractor,
        formatter=DeterministicFormatter(),
        config=cfg,
    )
