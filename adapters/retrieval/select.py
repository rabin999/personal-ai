"""S2 SELECT — turn ranked search hits into a clean, de-duplicated fetch list.

Drop junk before we spend a render: link-farms/aggregators, query-echo pages, and
duplicate domains (one page per domain — corroboration counts INDEPENDENT domains, so
two hits on the same site are one source). Order is preserved so the orchestrator can
fetch the top ``initial`` first and widen only if they don't corroborate (S2 rule).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from ports.search import SearchResult

# Aggregators / link-farms / SEO-spam hosts whose pages rarely carry a first-hand,
# verifiable fact. Not a credibility score (that is explicitly NOT our job, brief §5) —
# just "don't waste a fetch here". Kept small and boring on purpose.
_JUNK_DOMAINS = frozenset(
    {
        "pinterest.com",
        "quora.com",
        "answers.com",
        "ask.com",
        "ehow.com",
        "wikihow.com",
        "slideshare.net",
        "scribd.com",
        "coursehero.com",
        "facebook.com",
        "twitter.com",
        "x.com",
        "instagram.com",
        "tiktok.com",
        "youtube.com",
        "reddit.com",
    }
)

# Non-HTML endpoints we cannot render for a fact (S5/edge: PDF/non-HTML → skip cleanly).
_SKIP_SUFFIXES = (".pdf", ".zip", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".mp4", ".mp3")


class Candidate:
    """A selected source: the URL to fetch plus the search snippet (a fallback fact if
    the fetch degrades to snippet-only)."""

    __slots__ = ("domain", "snippet", "title", "url")

    def __init__(self, url: str, domain: str, title: str, snippet: str) -> None:
        self.url = url
        self.domain = domain
        self.title = title
        self.snippet = snippet


def _domain(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def select_candidates(results: list[SearchResult], *, limit: int) -> list[Candidate]:
    """Filter + de-dupe search hits into at most ``limit`` fetch candidates.

    Pure and deterministic (no I/O) so selection is unit-testable on its own."""
    seen_domains: set[str] = set()
    out: list[Candidate] = []
    for r in results:
        url = (r.url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue  # answerBox entries with no link, mailto:, etc.
        domain = _domain(url)
        if not domain or domain in seen_domains or domain in _JUNK_DOMAINS:
            continue
        path = urlsplit(url).path.lower()
        if path.endswith(_SKIP_SUFFIXES):
            continue
        seen_domains.add(domain)
        out.append(Candidate(url=url, domain=domain, title=r.title, snippet=r.snippet))
        if len(out) >= limit:
            break
    return out
