"""S5 VALIDATE/RECENCY — extract a page date and judge staleness.

Recency is as important as content for a companion (brief §5). Two ideas the pipeline
keeps separate:

- **page-date vs fact-date.** A page published today can be ABOUT an old event, and an
  old page can still hold a stable fact. We extract the PAGE date here; whether the page
  actually contains the ANSWER is the extractor/cross-check job (topic-match ≠ answer-match).
- **time-sensitivity is a query property, not a page property.** For officeholder /
  price / "now" / "latest" queries a stale page is a NEGATIVE signal — the orchestrator
  down-weights or drops it. For an evergreen fact a missing/old date is fine.

All functions here are pure (regex + date math) so recency is unit- and mutation-testable
without a network.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

# Query markers that make freshness a HARD filter (brief §5 / web_search parity).
_TIME_SENSITIVE_MARKERS = (
    "now",
    "today",
    "tonight",
    "current",
    "currently",
    "latest",
    "live",
    "this week",
    "this month",
    "right now",
    "at the moment",
    "as of",
    "price",
    "ltp",
    "stock",
    "share price",
    "who is the",  # "who is the prime minister of X" — officeholder, changes
    "news",
    "score",
    "weather",
)

# <meta>/JSON-LD keys that carry a publish or update date, best-first.
_META_DATE_KEYS = (
    "article:published_time",
    "article:modified_time",
    "og:updated_time",
    "datePublished",
    "dateModified",
    "date",
    "publishdate",
    "pubdate",
    "lastmod",
    "sailthru.date",
)

_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# A date embedded in a URL path: /2024/03/17/ or /2024-03-17-
_URL_DATE_RE = re.compile(r"/(20\d{2})[/\-](0[1-9]|1[0-2])(?:[/\-](0[1-9]|[12]\d|3[01]))?")
_META_TAG_RE = re.compile(
    r'<meta[^>]+(?:property|name|itemprop)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
# JSON-LD "datePublished":"2024-05-01..."
_JSONLD_DATE_RE = re.compile(r'"(datePublished|dateModified)"\s*:\s*"([^"]+)"', re.IGNORECASE)


def infer_time_sensitive(query: str) -> bool:
    """Does this query's answer decay with time? (officeholder/price/'now'/'latest')."""
    q = query.lower()
    return any(m in q for m in _TIME_SENSITIVE_MARKERS)


def _iso_from(text: str) -> str | None:
    m = _ISO_RE.search(text)
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def extract_page_date(html: str, metadata: dict[str, Any], url: str) -> str | None:
    """Best-effort ISO-8601 page date from metadata → <meta> → JSON-LD → URL.

    Returns None when no date is extractable — the caller treats None as UNKNOWN and
    flags it, never as "fresh" (brief §5: no date → unknown, don't assume fresh)."""
    # 1. Structured metadata the crawler already parsed.
    for key in _META_DATE_KEYS:
        val = metadata.get(key)
        if isinstance(val, str) and (iso := _iso_from(val)):
            return iso

    # 2. Raw <meta> tags in the HTML.
    best: str | None = None
    for prop, content in _META_TAG_RE.findall(html or ""):
        if prop.lower() in _META_DATE_KEYS and (iso := _iso_from(content)):
            # published_time wins over modified; take the first strong hit.
            if prop.lower() in ("article:published_time", "datepublished"):
                return iso
            best = best or iso
    if best:
        return best

    # 3. JSON-LD blocks.
    for _key, val in _JSONLD_DATE_RE.findall(html or ""):
        if iso := _iso_from(val):
            return iso

    # 4. A date baked into the URL path.
    m = _URL_DATE_RE.search(url or "")
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3) or "01"
        if iso := _iso_from(f"{y}-{mo}-{d}"):
            return iso
    return None


def is_stale(
    page_date: str | None, *, time_sensitive: bool, stale_after_days: int, now: date | None = None
) -> bool:
    """A time-sensitive page older than the window is stale (a NEGATIVE signal).

    Only meaningful when ``time_sensitive`` — an old page about an old event is fine, so
    non-time-sensitive always returns False. A missing date on a time-sensitive query is
    treated as stale (unknown freshness can't be trusted as current)."""
    if not time_sensitive:
        return False
    if page_date is None:
        return True
    now = now or datetime.now().date()
    parsed = _iso_from(page_date)
    if parsed is None:
        return True
    age_days = (now - date.fromisoformat(parsed)).days
    return age_days > stale_after_days
