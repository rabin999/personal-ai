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

# Source-quality PREFERENCE (user ask: prefer official / authoritative / recent). This biases
# WHICH sources we fetch first — it is NOT a credibility gate (corroboration across independent
# domains stays the trust signal, brief §5). Government/academic + reference + established news get
# preferred; the provider's search rank (which carries its recency bias for time-sensitive queries)
# breaks ties. Kept small and boring; extend via config later.
_REFERENCE = frozenset({"wikipedia.org", "britannica.com"})
_MAJOR_NEWS = frozenset(
    {
        "reuters.com",
        "apnews.com",
        "bbc.com",
        "bbc.co.uk",
        "nytimes.com",
        "theguardian.com",
        "aljazeera.com",
        "cnn.com",
        "washingtonpost.com",
        "ft.com",
        "bloomberg.com",
        "npr.org",
        "abcnews.go.com",
        "cbsnews.com",
        "nbcnews.com",
        "kathmandupost.com",
        "thediplomat.com",
    }
)
# Official / academic TLD hints (substring match on the host).
_OFFICIAL_HINTS = (".gov", ".edu", ".gov.", ".ac.", ".mil", ".int")


def _in(domain: str, known: frozenset[str]) -> bool:
    """Match the domain or any subdomain of it (en.wikipedia.org → wikipedia.org)."""
    return any(domain == d or domain.endswith("." + d) for d in known)


def _authority(domain: str) -> int:
    """Higher = more authoritative/official. 0 = ordinary source (no penalty)."""
    if _in(domain, _REFERENCE) or any(h in domain for h in _OFFICIAL_HINTS):
        return 3
    if _in(domain, _MAJOR_NEWS):
        return 2
    return 0


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

    Pure and deterministic (no I/O) so selection is unit-testable on its own. De-dupes by
    domain and drops junk/non-HTML, then PREFERS authoritative/official sources (search rank —
    which carries the provider's recency bias — breaks ties), and returns the top ``limit``."""
    seen_domains: set[str] = set()
    ranked: list[tuple[int, Candidate]] = []
    for idx, r in enumerate(results):
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
        ranked.append((idx, Candidate(url=url, domain=domain, title=r.title, snippet=r.snippet)))
    # Authoritative first; original search order (recency-biased for time-sensitive queries) is
    # the stable tiebreaker. A preference, not a filter — nothing eligible is dropped for rank.
    ranked.sort(key=lambda t: (-_authority(t[1].domain), t[0]))
    return [c for _, c in ranked[:limit]]
