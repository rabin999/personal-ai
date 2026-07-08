"""Adapter: Brave web search, fallback provider (implements ports.search, spec §15)."""

import httpx

from ports.search import SearchProviderError, SearchResult

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
# Brave freshness codes (pd/pw/pm/py) for recency biasing (spec §15).
_FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}


class BraveSearch:
    name = "brave"
    # Brave Search API base tier ~ $3 / 1k queries.
    cost_per_query_usd = 0.003

    def __init__(self, api_key: str, timeout_s: float = 10.0) -> None:
        self._api_key = api_key
        self._timeout = timeout_s

    async def search(
        self, query: str, max_results: int = 8, *, recency: str | None = None
    ) -> list[SearchResult]:
        if not self._api_key:
            raise SearchProviderError("BRAVE_API_KEY not configured")
        params: dict[str, str | int] = {"q": query, "count": max_results}
        if recency and recency in _FRESHNESS:
            params["freshness"] = _FRESHNESS[recency]
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    _ENDPOINT,
                    headers={"X-Subscription-Token": self._api_key},
                    params=params,
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
