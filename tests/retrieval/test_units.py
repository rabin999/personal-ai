"""Pure-stage unit tests: select, recency, cross-check, deterministic extraction.

These are the isolated-logic tests (§6): no network, no LLM. They pin the two checks the
brief singles out (cross-corroboration, recency) at the unit level; test_pipeline.py
mutation-proves them end-to-end.
"""

from __future__ import annotations

from datetime import date

import pytest

from adapters.retrieval import crosscheck
from adapters.retrieval.crosscheck import cross_check
from adapters.retrieval.extract import (
    ExtractedClaim,
    extract_number_from_text,
    looks_numeric,
    normalize_number,
)
from adapters.retrieval.recency import extract_page_date, infer_time_sensitive, is_stale
from adapters.retrieval.select import select_candidates
from tests.retrieval.conftest import sr


# ── S2 SELECT ─────────────────────────────────────────────────────────────────
def test_select_dedupes_domains_and_drops_junk_and_pdf() -> None:
    results = [
        sr("A", "https://apnews.com/article/x", "s1"),
        sr("A2", "https://www.apnews.com/article/y", "s2"),  # same domain → dropped
        sr("junk", "https://pinterest.com/pin/1", "s3"),  # link-farm → dropped
        sr("pdf", "https://gov.np/report.pdf", "s4"),  # non-HTML → dropped
        sr("B", "https://reuters.com/world/z", "s5"),
        sr("nolink", "", "answerbox"),  # no url → dropped
    ]
    cands = select_candidates(results, limit=3)
    assert [c.domain for c in cands] == ["apnews.com", "reuters.com"]


def test_select_respects_limit_order() -> None:
    results = [sr(f"t{i}", f"https://d{i}.com/p", f"s{i}") for i in range(5)]
    cands = select_candidates(results, limit=2)
    assert [c.domain for c in cands] == ["d0.com", "d1.com"]


# ── S5 RECENCY ────────────────────────────────────────────────────────────────
def test_infer_time_sensitive() -> None:
    assert infer_time_sensitive("who is the current prime minister of Nepal")
    assert infer_time_sensitive("latest LTP of NABIL")
    assert not infer_time_sensitive("who wrote Pride and Prejudice")


def test_extract_page_date_from_meta_and_url_and_jsonld() -> None:
    html_meta = '<meta property="article:published_time" content="2025-03-04T10:00:00Z">'
    assert extract_page_date(html_meta, {}, "https://x.com/a") == "2025-03-04"
    html_ld = '{"@type":"NewsArticle","datePublished":"2024-11-20"}'
    assert extract_page_date(html_ld, {}, "https://x.com/a") == "2024-11-20"
    assert extract_page_date("", {}, "https://x.com/2023/06/15/story") == "2023-06-15"
    assert extract_page_date("", {}, "https://x.com/no-date") is None  # unknown, not "fresh"


def test_is_stale_only_when_time_sensitive() -> None:
    today = date(2026, 7, 11)
    old = "2019-01-01"
    # Non-time-sensitive: an old page is fine.
    assert not is_stale(old, time_sensitive=False, stale_after_days=120, now=today)
    # Time-sensitive: an old page is stale; a fresh one is not; unknown date is stale.
    assert is_stale(old, time_sensitive=True, stale_after_days=120, now=today)
    assert not is_stale("2026-06-01", time_sensitive=True, stale_after_days=120, now=today)
    assert is_stale(None, time_sensitive=True, stale_after_days=120, now=today)


# ── deterministic numeric extraction ─────────────────────────────────────────
def test_numeric_query_and_number_extraction() -> None:
    assert looks_numeric("current LTP of NABIL")
    assert looks_numeric("population of Japan")
    assert not looks_numeric("who is the president")
    assert extract_number_from_text("The LTP closed at Rs. 1,240.50 today") == "Rs. 1,240.50"
    assert normalize_number("Rs. 1,240.50") == "1240.5"
    assert normalize_number("1240.50") == "1240.5"  # canonical form ignores trailing zeros


# ── S6 CROSS-CHECK ────────────────────────────────────────────────────────────
def _claim(domain: str, answer: str, kind: str = "text", text: str = "") -> ExtractedClaim:
    return ExtractedClaim(
        domain=domain, url=f"https://{domain}/x", answer=answer, kind=kind, text=text or answer
    )


def test_cross_check_corroborated_needs_two_independent_domains() -> None:
    claims = [_claim("apnews.com", "Sushila Karki"), _claim("reuters.com", "Sushila Karki")]
    r = cross_check(claims)
    assert r.status == "corroborated"
    assert r.corroboration_count == 2
    assert r.answer == "Sushila Karki"


def test_cross_check_single_source() -> None:
    r = cross_check([_claim("apnews.com", "Sushila Karki")])
    assert r.status == "single_source"
    assert r.corroboration_count == 1


def test_cross_check_not_found() -> None:
    assert cross_check([]).status == "not_found"


def test_cross_check_conflicting_surfaces_both() -> None:
    claims = [_claim("a.com", "Alice"), _claim("b.com", "Bob")]
    r = cross_check(claims)
    assert r.status == "conflicting"
    assert {cl.answer for cl in r.clusters} == {"Alice", "Bob"}


def test_cross_check_fuzzy_same_claim_clusters() -> None:
    # "Sushila Karki" and "Sushila Karki is the PM" are ONE claim.
    claims = [_claim("a.com", "Sushila Karki"), _claim("b.com", "Sushila Karki is the PM")]
    assert cross_check(claims).status == "corroborated"


def test_cross_check_dedupes_syndicated_text() -> None:
    # Two domains carrying the SAME wire copy count as ONE source, not corroboration.
    wire = "Reuters — the central bank raised rates by fifty basis points on Tuesday morning."
    claims = [
        _claim("siteA.com", "rates raised", text=wire),
        _claim("siteB.com", "rates raised", text=wire),
    ]
    r = cross_check(claims)
    assert r.status == "single_source"  # syndication collapsed to one


def test_cross_check_majority_beats_lone_outlier() -> None:
    claims = [
        _claim("a.com", "Sushila Karki"),
        _claim("b.com", "Sushila Karki"),
        _claim("c.com", "KP Oli"),
    ]
    r = cross_check(claims)
    assert r.status == "corroborated"
    assert r.answer == "Sushila Karki"


# ── MUTATION-PROOF (unit level): the corroboration threshold is load-bearing ──
def test_mutation_corroboration_threshold_is_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """If cross-corroboration is broken (min → 1), a SINGLE source wrongly reads as
    corroborated. This proves the >=2 check is what stops it — flip it and it breaks."""
    single = [_claim("apnews.com", "Sushila Karki")]
    assert cross_check(single).status == "single_source"  # correct behaviour

    monkeypatch.setattr(crosscheck, "_CORROBORATION_MIN", 1)  # BREAK the check
    assert cross_check(single).status == "corroborated"  # now wrong → the check mattered
