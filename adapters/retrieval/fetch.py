"""S3 FETCH — render/scrape the selected pages via the Crawl4AI Docker service.

The pipeline talks to Crawl4AI through the :class:`PageFetcher` protocol, so the
orchestrator never knows whether the bytes came from a live Chromium render, a
recorded fixture, or a degraded no-op. Two implementations live here:

- :class:`Crawl4AIClient` — HTTP client for the Docker server on ``127.0.0.1:11235``
  (v0.9.1 ``POST /crawl/stream``, NDJSON). It lets Crawl4AI decide JS-render vs static,
  applies a per-URL timeout, and turns any per-source failure into a clean
  :class:`FetchedPage` with ``success=False`` — never an exception (D-9 degrade rule).
- If the whole server is unreachable the client returns ``server_down=True`` on every
  page so the orchestrator can degrade honestly to snippet-only instead of crashing.

API verified against docs.crawl4ai.com v0.9.x (see docs/VERIFIED_RETRIEVAL_REPORT.md):
the POST body uses the ``{"type": "BrowserConfig"/"CrawlerRunConfig", "params": {...}}``
dump shape; ``stream:true`` yields newline-delimited ``CrawlResult`` JSON with a final
``{"status":"completed"}`` marker.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class FetchedPage(BaseModel):
    """One page after fetch — success or a clean per-source failure, never a raise."""

    requested_url: str
    final_url: str = ""  # after redirects; S5 uses the FINAL url for date hints
    success: bool = False
    status_code: int | None = None
    raw_markdown: str = ""
    fit_markdown: str = ""  # relevance-filtered (BM25 to the query) when available
    html: str = ""  # kept for <meta> date extraction (S5)
    metadata: dict[str, Any] = Field(default_factory=dict)
    error_reason: str = ""  # populated when success is False
    server_down: bool = False  # the whole Crawl4AI service was unreachable

    @property
    def domain(self) -> str:
        host = urlsplit(self.final_url or self.requested_url).netloc.lower()
        return host[4:] if host.startswith("www.") else host

    def best_text(self) -> str:
        """Prefer the relevance-filtered markdown; fall back to raw."""
        return self.fit_markdown.strip() or self.raw_markdown.strip()


class PageFetcher(Protocol):
    """Query in, rendered pages out. Never raises for a source/dependency failure."""

    async def fetch(self, urls: list[str], query: str) -> list[FetchedPage]: ...


def _server_down_pages(urls: list[str], reason: str) -> list[FetchedPage]:
    return [
        FetchedPage(requested_url=u, success=False, error_reason=reason, server_down=True)
        for u in urls
    ]


class Crawl4AIClient:
    """HTTP client for the Crawl4AI Docker server (``POST /crawl/stream``, NDJSON)."""

    def __init__(
        self,
        base_url: str,
        api_token: str = "",
        *,
        page_timeout_ms: int = 12_000,
        fetch_deadline_ms: int = 20_000,
        max_concurrency: int = 4,
        word_count_threshold: int = 40,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = api_token
        self._page_timeout_ms = page_timeout_ms
        self._deadline_s = fetch_deadline_ms / 1000.0
        self._max_concurrency = max_concurrency
        self._word_count_threshold = word_count_threshold

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _payload(self, urls: list[str], query: str) -> dict[str, Any]:
        # BM25 relevance filter → result.markdown.fit_markdown scoped to the query;
        # word_count_threshold rejects thin blocks; excluded_tags strips chrome.
        return {
            "urls": urls,
            "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {
                    "stream": True,
                    "cache_mode": "bypass",
                    "page_timeout": self._page_timeout_ms,
                    "word_count_threshold": self._word_count_threshold,
                    "excluded_tags": ["nav", "footer", "header", "aside", "form"],
                    "markdown_generator": {
                        "type": "DefaultMarkdownGenerator",
                        "params": {
                            "content_filter": {
                                "type": "BM25ContentFilter",
                                "params": {"user_query": query},
                            }
                        },
                    },
                },
            },
        }

    async def fetch(self, urls: list[str], query: str) -> list[FetchedPage]:
        if not urls:
            return []
        out: dict[str, FetchedPage] = {
            u: FetchedPage(requested_url=u, success=False, error_reason="no result returned")
            for u in urls
        }
        try:
            async with (
                httpx.AsyncClient(timeout=self._deadline_s) as client,
                client.stream(
                    "POST",
                    f"{self._base_url}/crawl/stream",
                    headers=self._headers(),
                    json=self._payload(urls, query),
                ) as resp,
            ):
                if resp.status_code in (401, 403):
                    return _server_down_pages(urls, f"crawl4ai auth failed ({resp.status_code})")
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    page = _parse_ndjson_line(line)
                    if page is not None:
                        out[page.requested_url] = page
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            logger.warning("crawl4ai server unreachable at %s: %s", self._base_url, exc)
            return _server_down_pages(urls, f"crawl4ai unreachable: {exc}")
        except httpx.HTTPError as exc:
            # A read timeout mid-stream: keep whatever pages already arrived, mark the rest.
            logger.warning("crawl4ai stream error: %s", exc)
        return list(out.values())


def _parse_ndjson_line(line: str) -> FetchedPage | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("status") == "completed":
        return None
    if "url" not in obj and "requested_url" not in obj:
        return None
    return page_from_crawl_result(obj)


def page_from_crawl_result(obj: dict[str, Any]) -> FetchedPage:
    """Map a Crawl4AI ``CrawlResult`` JSON object → :class:`FetchedPage`.

    Shared by the live client and fixture loaders so fixtures exercise the SAME
    mapping the server output goes through (a fixture that skips the mapping is
    worthless). ``markdown`` is either a string (raw) or an object with
    ``raw_markdown`` / ``fit_markdown`` (when a content filter ran)."""
    requested = str(obj.get("requested_url") or obj.get("url") or "")
    final_url = str(obj.get("url") or requested)
    raw_md, fit_md = _split_markdown(obj.get("markdown"))
    success = bool(obj.get("success", False))
    return FetchedPage(
        requested_url=requested,
        final_url=final_url,
        success=success,
        status_code=obj.get("status_code"),
        raw_markdown=raw_md,
        fit_markdown=fit_md,
        html=str(obj.get("html") or obj.get("cleaned_html") or ""),
        metadata=obj.get("metadata") or {},
        error_reason="" if success else str(obj.get("error_message") or "crawl failed"),
    )


def _split_markdown(md: Any) -> tuple[str, str]:
    if isinstance(md, str):
        return md, ""
    if isinstance(md, dict):
        return str(md.get("raw_markdown") or ""), str(md.get("fit_markdown") or "")
    return "", ""
