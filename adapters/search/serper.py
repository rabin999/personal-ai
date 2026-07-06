"""Adapter: Serper web search, primary provider (implements ports.search, spec §15)."""

import httpx

from ports.search import SearchProviderError, SearchResult

_ENDPOINT = "https://google.serper.dev/search"


class SerperSearch:
    name = "serper"
    # google.serper.dev pricing ~ $0.30 / 1k queries.
    cost_per_query_usd = 0.0003

    def __init__(self, api_key: str, timeout_s: float = 10.0) -> None:
        self._api_key = api_key
        self._timeout = timeout_s

    async def search(self, query: str, max_results: int = 8) -> list[SearchResult]:
        if not self._api_key:
            raise SearchProviderError("SERPER_API_KEY not configured")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    _ENDPOINT,
                    headers={"X-API-KEY": self._api_key},
                    json={"q": query, "num": max_results},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SearchProviderError(f"serper: {exc}") from exc
        organic = response.json().get("organic", [])
        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
            )
            for item in organic[:max_results]
        ]
