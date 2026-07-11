"""S6 CROSS-CHECK — does the SAME fact appear on >= 2 INDEPENDENT domains?

Corroboration, NOT credibility scoring (brief §6 — we never rank sources by trust). The
one question: do independent domains agree on the extracted answer?

- Cluster claims by a normalized answer key (numbers canonicalised; text fuzzy-matched so
  "Sushila Karki" and "Sushila Karki is the PM" are ONE claim).
- De-dupe syndicated / near-identical page text across domains → counts as ONE source (a
  wire story on ten sites is not ten confirmations).
- Decide: corroborated (a cluster with >= 2 independent domains) / single_source (one
  domain has it) / conflicting (independent domains give different answers — surface all,
  choose none) / not_found (nobody answered).

This module is PURE (no I/O, no LLM) so the two checks that matter are mutation-provable:
flip ``_CORROBORATION_MIN`` and a single-source result stops reading as corroborated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from adapters.retrieval.extract import ExtractedClaim, normalize_number

# The invariant of S6: a fact is corroborated only with at least this many INDEPENDENT
# domains behind it. This single constant IS the cross-corroboration check — the mutation
# test flips it to 1 and the "single source must not read as corroborated" test goes red.
_CORROBORATION_MIN = 2

# Fuzzy-match threshold for treating two text answers as the same claim.
_SAME_CLAIM_JACCARD = 0.5
# Above this token-overlap two pages are treated as syndicated (same underlying text).
_SYNDICATION_JACCARD = 0.85
# ...but only compare page-text bodies long enough for overlap to MEAN syndication. Two
# sources that happen to give the same SHORT answer ("Sushila Karki") are corroboration,
# not a syndicated copy — never collapse them.
_SYNDICATION_MIN_TOKENS = 6

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _norm_key(claim: ExtractedClaim) -> str:
    if claim.kind == "number":
        return normalize_number(claim.answer) or claim.answer.lower().strip()
    return " ".join(sorted(_tokens(claim.answer)))


@dataclass
class Cluster:
    """One distinct answer and the independent domains that back it."""

    answer: str  # representative (shortest) phrasing
    kind: str
    domains: list[str] = field(default_factory=list)
    claims: list[ExtractedClaim] = field(default_factory=list)


@dataclass
class CrossCheckResult:
    status: str  # corroborated | single_source | conflicting | not_found
    answer: str | None
    corroboration_count: int
    clusters: list[Cluster]
    kind: str = "text"


def _same_claim(a: ExtractedClaim, b: ExtractedClaim) -> bool:
    if a.kind == "number" or b.kind == "number":
        return _norm_key(a) == _norm_key(b)
    ta, tb = _tokens(a.answer), _tokens(b.answer)
    return _jaccard(ta, tb) >= _SAME_CLAIM_JACCARD or ta <= tb or tb <= ta


def _dedupe_syndicated(claims: list[ExtractedClaim]) -> list[ExtractedClaim]:
    """Collapse near-identical page text from different domains to a single source."""
    kept: list[ExtractedClaim] = []
    kept_tokens: list[set[str]] = []
    for c in claims:
        toks = _tokens(c.text or c.answer)
        long_enough = len(toks) >= _SYNDICATION_MIN_TOKENS
        if long_enough and any(_jaccard(toks, kt) >= _SYNDICATION_JACCARD for kt in kept_tokens):
            continue  # syndicated copy of an already-counted source
        kept.append(c)
        kept_tokens.append(toks)
    return kept


def _cluster(claims: list[ExtractedClaim]) -> list[Cluster]:
    clusters: list[Cluster] = []
    for c in claims:
        placed = False
        for cl in clusters:
            if _same_claim(c, cl.claims[0]):
                if c.domain not in cl.domains:  # independent domains only
                    cl.domains.append(c.domain)
                cl.claims.append(c)
                if len(c.answer) < len(cl.answer):
                    cl.answer = c.answer
                placed = True
                break
        if not placed:
            clusters.append(Cluster(answer=c.answer, kind=c.kind, domains=[c.domain], claims=[c]))
    clusters.sort(key=lambda cl: len(cl.domains), reverse=True)
    return clusters


def cross_check(claims: list[ExtractedClaim]) -> CrossCheckResult:
    """The corroboration decision. Assumes stale claims already dropped by S5."""
    claims = _dedupe_syndicated([c for c in claims if c.answer.strip()])
    if not claims:
        return CrossCheckResult("not_found", None, 0, [])

    clusters = _cluster(claims)
    top = clusters[0]

    if len(clusters) == 1:
        if len(top.domains) >= _CORROBORATION_MIN:
            return CrossCheckResult(
                "corroborated", top.answer, len(top.domains), clusters, top.kind
            )
        return CrossCheckResult("single_source", top.answer, 1, clusters, top.kind)

    # More than one distinct answer survived. A clear majority (>= min independent domains
    # AND strictly more than the runner-up) is corroborated; otherwise the sources
    # genuinely disagree and we surface the conflict rather than silently pick one.
    runner_up = clusters[1]
    if len(top.domains) >= _CORROBORATION_MIN and len(top.domains) > len(runner_up.domains):
        return CrossCheckResult("corroborated", top.answer, len(top.domains), clusters, top.kind)
    return CrossCheckResult("conflicting", None, len(top.domains), clusters, top.kind)
