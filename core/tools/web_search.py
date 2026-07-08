"""Web Search (spec §15): cached, summarized, provider-fallback search.

Runs as a §14 background task — NEVER inside the conversational LLM's
generation. Cache-first (per-query-type TTL), Serper → Brave fallback,
cheap-LLM summarization so raw SERPs never reach the main context, and
cost logging on both the hit ($0, cache_hit) and miss (real cost) paths.
"""

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from core.cost import CostEntry, CostLedger, CostMetadata
from ports.doc_store import DocStore
from ports.llm import LLM, LLMUnavailable
from ports.queue import QueuedTask
from ports.search import SearchProvider, SearchProviderError, SearchResult

logger = logging.getLogger(__name__)

SEARCH_CACHE_COLLECTION = "search_cache"

# Time-sensitive queries stale fast; stable facts hold for a day.
SHORT_TTL_S = 15 * 60
LONG_TTL_S = 24 * 3600
_TIME_SENSITIVE_MARKERS = (
    "today",
    "now",
    "latest",
    "news",
    "price",
    "open",
    "score",
    "current",
    "this week",
    "tonight",
    "live",
)

_SUMMARIZE_INSTRUCTIONS = (
    "Summarize these web search results for a voice conversation: 2-4 short "
    "sentences, the concrete facts only, no URLs, no markdown. PRIORITISE the MOST "
    "RECENT information — when results carry dates, lead with the latest and mention "
    "how recent it is ('as of today', 'earlier this week'); if some results look "
    "outdated, ignore them in favour of the newest. If results genuinely conflict or "
    "are unclear, say so briefly rather than guessing."
)


class SearchOutcome(BaseModel):
    summary: str
    sources: list[SearchResult]
    cache_hit: bool = False
    provider: str


class WebSearch:
    def __init__(
        self,
        docs: DocStore,
        llm: LLM,
        primary: SearchProvider,
        fallback: SearchProvider | None = None,
        ledger: CostLedger | None = None,
    ) -> None:
        self._docs = docs
        self._llm = llm
        self._primary = primary
        self._fallback = fallback
        self._ledger = ledger

    async def run(self, query: str, user_id: str, session_id: str | None = None) -> SearchOutcome:
        # An empty/blank query is a no-op — never spend a paid provider call
        # (providers 400 on it anyway) or a cache slot on it.
        if not query.strip():
            return SearchOutcome(summary="", sources=[], provider="none")

        # Breaking / current-event queries (recency "week") BYPASS the cache — a
        # fast-moving story ("plane missing", "latest news") must always re-fetch, or a
        # result cached minutes ago masks the real latest. Stable queries still cache.
        recency = _recency_for(query)
        cache_key = _hash(query)
        if recency not in ("day", "week"):  # current/breaking → always re-fetch, never cached
            cached = await self._read_cache(cache_key, query)
            if cached is not None:
                self._log_cost(user_id, session_id, provider=cached.provider, cost=0.0, hit=True)
                return cached

        try:
            results, provider = await self._search_with_fallback(query)
        except SearchProviderError as exc:
            # Never block or error out to the user — say plainly what failed (§16).
            logger.warning("web search failed for %r: %s", query, exc)
            return SearchOutcome(
                summary=(
                    "I tried to look that up but the web search didn't go through just "
                    "now — I couldn't get current results on it."
                ),
                sources=[],
                provider="none",
            )
        summary = await self._summarize(user_id, session_id, query, results)
        outcome = SearchOutcome(summary=summary, sources=results, provider=provider.name)

        await self._docs.put(
            SEARCH_CACHE_COLLECTION,
            cache_key,
            {
                "query": query,
                "cached_at": datetime.now(UTC).isoformat(),
                "ttl_s": _ttl_for(query),
                "outcome": outcome.model_dump(),
            },
        )
        self._log_cost(
            user_id,
            session_id,
            provider=provider.name,
            cost=provider.cost_per_query_usd,
            hit=False,
        )
        return outcome

    def as_task_handler(self) -> Any:
        """§14 worker handler: search runs detached from the conversation."""

        async def handle(task: QueuedTask) -> dict[str, Any]:
            outcome = await self.run(
                str(task.params.get("query", "")), task.user_id, task.session_id
            )
            return outcome.model_dump()

        return handle

    async def _search_with_fallback(self, query: str) -> tuple[list[SearchResult], SearchProvider]:
        recency = _recency_for(query)  # §15: bias to LATEST unless a date/timeline is given
        try:
            return await self._primary.search(query, recency=recency), self._primary
        except SearchProviderError as exc:
            logger.warning("primary search failed (%s); trying fallback", exc)
            if self._fallback is None:
                raise
            return await self._fallback.search(query, recency=recency), self._fallback

    async def _summarize(
        self, user_id: str, session_id: str | None, query: str, results: list[SearchResult]
    ) -> str:
        if not results:
            return "The search returned nothing useful."
        payload = json.dumps([r.model_dump() for r in results])
        try:
            completion = await self._llm.complete(
                user_id,
                [
                    {"role": "system", "content": _SUMMARIZE_INSTRUCTIONS},
                    {"role": "user", "content": f"Query: {query}\nResults: {payload}"},
                ],
                "simple",
                session_id=session_id,
                purpose="search_summarize",
            )
            if completion.text.strip():
                return completion.text.strip()
        except LLMUnavailable:
            logger.warning("summarizer unavailable; falling back to top snippets")
        return " ".join(r.snippet for r in results[:3])

    async def _read_cache(self, cache_key: str, query: str) -> SearchOutcome | None:
        doc = await self._docs.get(SEARCH_CACHE_COLLECTION, cache_key)
        if doc is None:
            return None
        cached_at = datetime.fromisoformat(doc["cached_at"])
        age_s = (datetime.now(UTC) - cached_at).total_seconds()
        if age_s > float(doc.get("ttl_s", LONG_TTL_S)):
            return None
        outcome = SearchOutcome.model_validate(doc["outcome"])
        outcome.cache_hit = True
        return outcome

    def _log_cost(
        self, user_id: str, session_id: str | None, *, provider: str, cost: float, hit: bool
    ) -> None:
        if self._ledger is None:
            return
        self._ledger.log(
            CostEntry(
                user_id=user_id,
                component="search",
                provider=provider,
                units={"queries": 1},
                cost_usd=cost,
                metadata=CostMetadata(session_id=session_id, cache_hit=hit),
            )
        )


def _hash(query: str) -> str:
    return hashlib.sha256(query.strip().lower().encode()).hexdigest()


def _ttl_for(query: str) -> int:
    lowered = query.lower()
    if any(marker in lowered for marker in _TIME_SENSITIVE_MARKERS):
        return SHORT_TTL_S
    return LONG_TTL_S


# A query that names a specific past date / year / historical window wants THAT period,
# not "latest" — respect it (don't force a recency filter).
_HISTORICAL = re.compile(
    r"\b(1\d{3}|20\d{2})\b|\b(last year|years? ago|back in|history of|in the past|previously|"
    r"used to|originally|founded|born in|invented|ancient|decades? ago)\b",
    re.IGNORECASE,
)
# "Right now" words — the user wants what's happening THIS moment → tightest window
# (past 24h), so "recent" genuinely means current, not week-old (user feedback).
_NOW = (
    "right now", "today", "tonight", "now", "currently", "at the moment", "this hour",
    "breaking", "latest", "just now", "just happened", "as of", "this morning",
    "this afternoon", "this evening", "live",
)  # fmt: skip
# Unfolding-story / event words — current but may span a few days → past week.
_BREAKING = (
    "happening", "missing", "crash", "crashed", "killed", "dead", "died", "attack",
    "earthquake", "wildfire", "outage", "explosion", "shooting", "election", "won",
    "wins", "update", "developing", "so far", "news", "headline",
)  # fmt: skip


def _recency_for(query: str) -> str | None:
    """How fresh the results should be (spec §15). "Recent" means CURRENT: explicit
    now-words get the past DAY, unfolding events get the past WEEK, everything else the
    past MONTH — UNLESS the query names a specific historical date/timeline (then no
    filter). Never returns week-old news for a "right now" question."""
    lowered = query.lower()
    if _HISTORICAL.search(query):
        return None  # a specific past period → no freshness filter
    if any(m in lowered for m in _NOW):
        return "day"  # "right now / today / latest / breaking" → past 24h
    if any(m in lowered for m in _TIME_SENSITIVE_MARKERS) or any(m in lowered for m in _BREAKING):
        return "week"  # an unfolding story that may span a few days
    return "month"  # default: bias to the last month so "online" means recent
