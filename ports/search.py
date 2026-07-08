"""Port: web search providers — Serper primary, Brave fallback (spec §15)."""

from typing import Protocol

from pydantic import BaseModel


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str


class SearchProviderError(Exception):
    """Provider unreachable or returned an error — caller may fall back."""


class SearchProvider(Protocol):
    name: str
    cost_per_query_usd: float

    async def search(
        self, query: str, max_results: int = 8, *, recency: str | None = None
    ) -> list[SearchResult]:
        """Run one web search; raise SearchProviderError on failure.

        ``recency`` biases toward fresh results (spec §15 freshness): one of "day",
        "week", "month", "year", or None for no time filter. Providers map it to their
        own param (Serper ``tbs=qdr:*``, Brave ``freshness``)."""
        ...
