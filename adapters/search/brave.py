"""Adapter: Brave web search, fallback provider (implements ports.search, spec §15)."""

import httpx

from ports.search import SearchProviderError, SearchResult

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveSearch:
    name = "brave"
    # Brave Search API base tier ~ $3 / 1k queries.
    cost_per_query_usd = 0.003

    def __init__(self, api_key: str, timeout_s: float = 10.0) -> None:
        self._api_key = api_key
        self._timeout = timeout_s

    async def search(self, query: str, max_results: int = 8) -> list[SearchResult]:
        if not self._api_key:
            raise SearchProviderError("BRAVE_API_KEY not configured")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    _ENDPOINT,
                    headers={"X-Subscription-Token": self._api_key},
                    params={"q": query, "count": max_results},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SearchProviderError(f"brave: {exc}") from exc
        results = response.json().get("web", {}).get("results", [])
        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("description", ""),
            )
            for item in results[:max_results]
        ]
