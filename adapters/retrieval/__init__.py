"""Verified-retrieval adapter package (implements ``ports.retrieval.RetrievalPort``).

The concrete Crawl4AI pipeline lives here, behind the port — ``core/`` never imports it
(hexagonal boundary, design §17.2). :func:`build_crawl4ai_retrieval` is the composition
root's one-call wiring: inject the shared search provider + LLM + Cost Ledger and get a
ready ``RetrievalPort``.
"""

from __future__ import annotations

from adapters.retrieval.config import RetrievalConfig
from adapters.retrieval.crawl4ai_adapter import Crawl4AIRetrieval
from adapters.retrieval.extract import DeterministicThenLLMExtractor
from adapters.retrieval.fetch import Crawl4AIClient, FetchedPage, PageFetcher
from adapters.retrieval.format import LLMFormatter
from adapters.retrieval.trace import RetrievalTracer
from core.cost import CostLedger
from core.observability.logger import StructuredLogger
from ports.llm import LLM
from ports.retrieval import RetrievalPort
from ports.search import SearchProvider


def build_crawl4ai_retrieval(
    *,
    search: SearchProvider,
    llm: LLM,
    user_id: str,
    ledger: CostLedger | None = None,
    session_id: str | None = None,
    config: RetrievalConfig | None = None,
    fetcher: PageFetcher | None = None,
    logs: StructuredLogger | None = None,
) -> RetrievalPort:
    """Wire the Crawl4AI verified-retrieval pipeline behind the port.

    ``user_id`` is the resolved User-Context id (invariant 2) — the extractor/formatter
    LLM calls and their cost entries are scoped to it. ``fetcher`` defaults to the
    Crawl4AI Docker client on ``127.0.0.1:11235``; pass a fake for tests. ``logs`` (the
    project's structured logger, whose trace-store sink tags each span with the bound turn)
    receives a per-stage span for every step — omit it for a silent standalone run."""
    cfg = config or RetrievalConfig()
    tracer = RetrievalTracer(logs=logs, user_id=user_id, session_id=session_id)
    page_fetcher = fetcher or Crawl4AIClient(
        base_url=cfg.base_url,
        api_token=cfg.api_token,
        page_timeout_ms=cfg.page_timeout_ms,
        fetch_deadline_ms=cfg.fetch_deadline_ms,
        max_concurrency=cfg.max_concurrency,
        word_count_threshold=cfg.word_count_threshold,
    )
    extractor = DeterministicThenLLMExtractor(
        llm=llm,
        user_id=user_id,
        model=cfg.formatter_model,
        temperature=cfg.formatter_temperature,
        session_id=session_id,
    )
    formatter = LLMFormatter(
        llm=llm,
        user_id=user_id,
        model=cfg.formatter_model,
        temperature=cfg.formatter_temperature,
        ledger=ledger,
        session_id=session_id,
    )
    return Crawl4AIRetrieval(
        search=search,
        fetcher=page_fetcher,
        extractor=extractor,
        formatter=formatter,
        config=cfg,
        tracer=tracer,
    )


__all__ = [
    "Crawl4AIClient",
    "Crawl4AIRetrieval",
    "FetchedPage",
    "RetrievalConfig",
    "build_crawl4ai_retrieval",
]
