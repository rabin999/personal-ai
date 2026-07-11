"""Crawl4AI verified-retrieval adapter — implements ``ports.retrieval.RetrievalPort``.

query ─► SEARCH ─► SELECT ─► FETCH ─► EXTRACT ─► VALIDATE(recency) ─► CROSS-CHECK ─►
FORMAT ─► VerifiedResult.

The adapter OWNS orchestration and timing; each stage is a small pure/injectable unit
(``select`` / ``recency`` / ``extract`` / ``crosscheck`` / ``format``) so the harness can
drive the whole pipeline deterministically and mutation-test the two checks that matter.
It obeys the D-9 split: a source/dependency failure DEGRADES (per-source ``errors`` or an
honest ``status="error"``); only a real bug inside the pipeline raises
:class:`VerifiedRetrievalError`.

Deployment: fetch goes through the :class:`~adapters.retrieval.fetch.PageFetcher` port,
whose default implementation is the Crawl4AI Docker service on ``127.0.0.1:11235`` — the
render engine (Playwright/Chromium) stays out of the app venv. See
``docs/VERIFIED_RETRIEVAL_REPORT.md``.
"""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter

from adapters.retrieval.config import RetrievalConfig
from adapters.retrieval.crosscheck import CrossCheckResult, cross_check
from adapters.retrieval.extract import AnswerExtractor, ExtractedClaim, is_thin
from adapters.retrieval.fetch import FetchedPage, PageFetcher
from adapters.retrieval.format import Formatter
from adapters.retrieval.recency import extract_page_date, infer_time_sensitive, is_stale
from adapters.retrieval.select import Candidate, select_candidates
from adapters.retrieval.trace import NOOP_TRACER, RetrievalTracer
from core.errors import PROGRAMMING_ERRORS
from ports.retrieval import (
    ConflictClaim,
    Recency,
    SourceError,
    SourceRef,
    Timings,
    VerifiedResult,
    VerifiedRetrievalError,
)
from ports.search import SearchProvider, SearchProviderError

logger = logging.getLogger(__name__)


class _Fetched:
    """A page that survived S4/S5, carrying its extracted date + staleness."""

    __slots__ = ("candidate", "page", "page_date", "stale")

    def __init__(
        self, page: FetchedPage, candidate: Candidate, page_date: str | None, stale: bool
    ) -> None:
        self.page = page
        self.candidate = candidate
        self.page_date = page_date
        self.stale = stale


class Crawl4AIRetrieval:
    """Verified retrieval behind the port. Inject the search provider, the page fetcher,
    a per-source answer extractor, and the voice formatter (all swappable)."""

    def __init__(
        self,
        *,
        search: SearchProvider,
        fetcher: PageFetcher,
        extractor: AnswerExtractor,
        formatter: Formatter,
        config: RetrievalConfig | None = None,
        tracer: RetrievalTracer | None = None,
    ) -> None:
        self._search = search
        self._fetcher = fetcher
        self._extractor = extractor
        self._formatter = formatter
        self._cfg = config or RetrievalConfig()
        self._tracer = tracer or NOOP_TRACER

    async def verify(
        self,
        query: str,
        *,
        time_sensitive: bool | None = None,
        want_json: bool = False,
        max_sources: int = 3,
        budget_ms: int | None = None,
    ) -> VerifiedResult:
        if not isinstance(query, str):  # a real bug in the caller — fail loudly (D-9)
            raise VerifiedRetrievalError(f"query must be str, got {type(query)!r}")
        # Resilience (D-9): our own bugs FAIL LOUDLY as VerifiedRetrievalError; an unexpected
        # dependency blow-up DEGRADES to an honest error result — the turn never crashes.
        try:
            return await self._run(
                query.strip(),
                time_sensitive=time_sensitive,
                want_json=want_json,
                max_sources=max_sources,
                budget_ms=budget_ms,
            )
        except VerifiedRetrievalError:
            raise
        except PROGRAMMING_ERRORS as exc:
            self._tracer.event(
                "bug", "pipeline bug — failing loudly", level="error", error=repr(exc)
            )
            raise VerifiedRetrievalError(f"retrieval pipeline bug: {exc!r}") from exc
        except Exception as exc:  # unexpected dependency failure — degrade, don't crash
            self._tracer.event(
                "degrade",
                "unexpected dependency failure — degraded honestly",
                level="error",
                error=repr(exc),
            )
            logger.warning("verified-retrieval degraded on unexpected error: %s", exc)
            return _error_result(f"unexpected failure: {exc}", Timings())

    async def _run(
        self,
        query: str,
        *,
        time_sensitive: bool | None,
        want_json: bool,
        max_sources: int,
        budget_ms: int | None,
    ) -> VerifiedResult:
        started = perf_counter()
        ts = infer_time_sensitive(query) if time_sensitive is None else time_sensitive
        timings = Timings()
        errors: list[SourceError] = []
        self._tracer.event(
            "start",
            "verify query",
            query=query,
            time_sensitive=ts,
            want_json=want_json,
            budget_ms=budget_ms,
        )

        if not query:
            return _empty("not_found", timings)

        # ── S1 SEARCH: ranked links from the shared search provider ──────────
        t = perf_counter()
        with self._tracer.span("search", "web search for ranked links", query=query) as sp:
            try:
                results = await self._search.search(
                    query, max_results=8, recency="week" if ts else None
                )
            except SearchProviderError as exc:
                sp["error"] = str(exc)
                logger.warning("verified-retrieval search failed: %s", exc)
                timings.total_ms = _ms(started)
                return _error_result(f"search unavailable: {exc}", timings)
            sp["results"] = len(results)
        timings.search_ms = _ms(t)

        # ── S2 SELECT: drop junk/dupes, order candidates to fetch ────────────
        with self._tracer.span("select", "filter + de-dupe search hits to fetch list") as sp:
            candidates = select_candidates(results, limit=max_sources)
            # Persist the ranked shortlist (title + snippet), not just domains, so the
            # detailed trace can show "considered N results" with what each one was.
            sp["candidates"] = [
                {"rank": i, "domain": c.domain, "title": c.title, "snippet": c.snippet}
                for i, c in enumerate(candidates, start=1)
            ]
        if not candidates:
            timings.total_ms = _ms(started)
            return _empty("not_found", timings)

        # ── S3/S4/S5 FETCH → EXTRACT → RECENCY, adaptive widening ────────────
        fetched: list[_Fetched] = []
        claims: list[ExtractedClaim] = []
        used_candidates: list[Candidate] = []
        server_down = False
        idx = 0
        # Start with the top `initial_sources`; widen to `max_sources` only if not corroborated.
        wave_bounds = [min(self._cfg.initial_sources, len(candidates)), len(candidates)]
        cc = CrossCheckResult("not_found", None, 0, [])
        for wave_no, bound in enumerate(wave_bounds, start=1):
            wave = candidates[idx:bound]
            idx = bound
            if not wave:
                continue
            with self._tracer.span(
                "fetch",
                "render + scrape selected pages (Crawl4AI)",
                wave=wave_no,
                urls=[c.url for c in wave],
            ) as sp:
                new_fetched, new_errors, down = await self._fetch_wave(query, wave, ts, timings)
                sp["survived"] = [f.page.domain for f in new_fetched]
                sp["errors"] = [e.reason for e in new_errors]
            server_down = server_down or down
            errors.extend(new_errors)
            fetched.extend(new_fetched)
            used_candidates.extend(wave)
            with self._tracer.span("extract", "extract per-source claim + recency filter") as sp:
                claims = await self._extract_claims(query, fetched, timings)
                sp["claims"] = [{"domain": c.domain, "answer": c.answer} for c in claims]
            with self._tracer.span("cross_check", "corroborate across independent domains") as sp:
                cc = cross_check(claims)
                sp["status"] = cc.status
                sp["corroboration"] = cc.corroboration_count
            if cc.status == "corroborated":
                self._tracer.event("widen", "corroborated — stop widening", wave=wave_no)
                break
            if budget_ms is not None and _ms(started) > budget_ms:
                self._tracer.event(
                    "budget",
                    "fast-path budget spent — stop widening",
                    elapsed_ms=round(_ms(started), 1),
                    budget_ms=budget_ms,
                )
                break

        # ── Degrade: whole crawler down → snippet-only fallback (honest, low conf) ──
        snippet_only = False
        if server_down and not claims:
            snippet_only = True
            with self._tracer.span("degrade", "crawler unreachable — snippet-only fallback"):
                claims = await self._extract_claims(
                    query, self._snippet_pages(used_candidates), timings
                )
                cc = cross_check(claims)
            errors.append(SourceError(url="crawl4ai", reason="crawler down — served from snippets"))

        # ── Build the VerifiedResult ─────────────────────────────────────────
        result = self._assemble(cc, fetched, claims, ts, errors, timings, snippet_only)

        # Fast-path budget overrun → mark partial (contract has no flag; noted in errors).
        if budget_ms is not None and _ms(started) > budget_ms:
            result.errors.append(
                SourceError(url="(pipeline)", reason="partial: budget_ms exceeded")
            )
            result.confidence *= 0.7

        # ── S7 FORMAT: small fast LLM → grounded spoken text ─────────────────
        with self._tracer.span(
            "format", "format verified finding to voice (small LLM)", status=result.status
        ) as sp:
            voice, structured = await self._formatter.format(query, result, want_json=want_json)
            sp["voice"] = voice
        result.formatted_voice = voice
        result.formatted_json = structured
        result.timings.total_ms = _ms(started)
        self._tracer.event(
            "done",
            "verified",
            status=result.status,
            confidence=result.confidence,
            total_ms=round(result.timings.total_ms, 1),
        )
        return result

    # ── stages ───────────────────────────────────────────────────────────────
    async def _fetch_wave(
        self, query: str, wave: list[Candidate], ts: bool, timings: Timings
    ) -> tuple[list[_Fetched], list[SourceError], bool]:
        t = perf_counter()
        pages = await self._fetcher.fetch([c.url for c in wave], query)
        timings.fetch_ms += _ms(t)
        by_url = {c.url: c for c in wave}
        out: list[_Fetched] = []
        errors: list[SourceError] = []
        server_down = False
        for page in pages:
            cand = by_url.get(page.requested_url) or _guess_candidate(page, wave)
            if page.server_down:
                server_down = True
                continue
            if not page.success:
                errors.append(SourceError(url=page.requested_url, reason=page.error_reason))
                continue
            if is_thin(page, self._cfg.word_count_threshold):
                errors.append(SourceError(url=page.requested_url, reason="thin/blocked content"))
                continue
            page_date = extract_page_date(page.html, page.metadata, page.final_url)
            stale = is_stale(
                page_date, time_sensitive=ts, stale_after_days=self._cfg.stale_after_days
            )
            out.append(_Fetched(page, cand, page_date, stale))
        return out, errors, server_down

    async def _extract_claims(
        self, query: str, fetched: list[_Fetched], timings: Timings
    ) -> list[ExtractedClaim]:
        # Recency filter (S5): on a time-sensitive query, prefer FRESH pages; drop stale
        # ones — UNLESS every page is stale, in which case keep them (corroborated-but-stale,
        # flagged later) rather than silently return nothing.
        pool = fetched
        fresh = [f for f in fetched if not f.stale]
        if fresh and len(fresh) != len(fetched):
            pool = fresh  # fresh corroboration beats stale; stale dropped as a negative signal
        t = perf_counter()
        results = await asyncio.gather(*(self._extractor.extract(query, f.page) for f in pool))
        timings.extract_ms += _ms(t)
        return [c for c in results if c is not None]

    def _assemble(
        self,
        cc: CrossCheckResult,
        fetched: list[_Fetched],
        claims: list[ExtractedClaim],
        ts: bool,
        errors: list[SourceError],
        timings: Timings,
        snippet_only: bool,
    ) -> VerifiedResult:
        # Every source that fed a surviving claim → provenance.
        claim_domains = {c.domain for c in claims}
        by_domain = {f.page.domain: f for f in fetched}
        sources: list[SourceRef] = []
        dates: list[str] = []
        any_stale = False
        for c in claims:
            f = by_domain.get(c.domain)
            pd = f.page_date if f else None
            if f and f.stale:
                any_stale = True
            if pd:
                dates.append(pd)
            sources.append(
                SourceRef(url=c.url, domain=c.domain, published_date=pd, snippet=c.answer[:200])
            )
        # De-dupe provenance by domain, preserving order.
        seen: set[str] = set()
        deduped: list[SourceRef] = []
        for s in sources:
            if s.domain not in seen:
                seen.add(s.domain)
                deduped.append(s)
        sources = deduped

        recency = Recency(
            most_recent_source_date=max(dates) if dates else None,
            is_time_sensitive=ts,
            is_stale=ts and any_stale and cc.status in ("corroborated", "single_source"),
        )
        conflict = None
        if cc.status == "conflicting":
            conflict = [
                ConflictClaim(source=cl.domains[0] if cl.domains else "unknown", claim=cl.answer)
                for cl in cc.clusters[:3]
            ]

        all_failed = cc.status == "not_found" and not claim_domains and bool(errors)
        status = "error" if all_failed else cc.status
        answer = None if status == "error" else cc.answer
        confidence = _confidence(cc.status, cc.corroboration_count, recency.is_stale, snippet_only)

        return VerifiedResult(
            status=status,
            answer=answer,
            confidence=confidence,
            sources=sources,
            corroboration_count=cc.corroboration_count if cc.status == "corroborated" else 0,
            recency=recency,
            conflict=conflict,
            timings=timings,
            errors=errors,
        )

    def _snippet_pages(self, candidates: list[Candidate]) -> list[_Fetched]:
        out: list[_Fetched] = []
        for c in candidates:
            page = FetchedPage(
                requested_url=c.url, final_url=c.url, success=True, raw_markdown=c.snippet
            )
            out.append(_Fetched(page, c, None, False))
        return out


# ── helpers ───────────────────────────────────────────────────────────────────
def _ms(since: float) -> float:
    return (perf_counter() - since) * 1000.0


def _guess_candidate(page: FetchedPage, wave: list[Candidate]) -> Candidate:
    for c in wave:
        if c.domain == page.domain:
            return c
    return Candidate(url=page.final_url, domain=page.domain, title="", snippet="")


def _confidence(status: str, count: int, stale: bool, snippet_only: bool) -> float:
    base = {
        "corroborated": min(0.75 + 0.08 * max(count - 2, 0), 0.95),
        "single_source": 0.5,
        "conflicting": 0.3,
        "not_found": 0.0,
        "error": 0.0,
    }.get(status, 0.0)
    if stale:
        base *= 0.55
    if snippet_only:
        base = min(base * 0.5, 0.3)
    return round(base, 3)


def _empty(status: str, timings: Timings) -> VerifiedResult:
    voice = "I looked, but I couldn't find a reliable source for that, so I won't guess."
    return VerifiedResult(
        status=status, answer=None, confidence=0.0, timings=timings, formatted_voice=voice
    )


def _error_result(reason: str, timings: Timings) -> VerifiedResult:
    return VerifiedResult(
        status="error",
        answer=None,
        confidence=0.0,
        timings=timings,
        errors=[SourceError(url="(search)", reason=reason)],
        formatted_voice="I tried to check that, but the lookup didn't go through just now.",
    )
