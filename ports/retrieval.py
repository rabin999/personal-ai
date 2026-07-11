"""Port: verified retrieval — read the actual pages, cross-check, return a grounded result.

A standalone verification pipeline that sits BEHIND plain search (design §15 + the
"read the page, don't trust the snippet" lesson that produced a crypto-token answer for a
NEPSE ticker and stale officeholders). Given a query the engine has ALREADY built — the
entity is resolved upstream (`_build_search_query`); this port does not re-resolve "OP" —
it fetches and reads the real pages, checks whether the answer is corroborated across
independent sources and is current, and returns a typed :class:`VerifiedResult` the voice
engine can speak.

Boundary: ``core/`` depends only on this port; the concrete Crawl4AI adapter lives in
``adapters/retrieval/`` and is swappable (design §17.3). This module is the ONE shared
surface between the engine and the retrieval build — it is **frozen**: the adapter and its
harness build against these types, and the engine's tool layer consumes them. Changing any
type here means reconciling BOTH sides, so treat it as an interface contract, not an
implementation detail.

Two hard rules the port encodes (brief §4/§5):
- Zero and conflicting results are FIRST-CLASS outputs, not exceptions. ``not_found`` /
  ``conflicting`` are ordinary return values; nothing is fabricated to fill a gap.
- Source/dependency failures DEGRADE (per-source ``errors`` or ``status="error"``); only a
  programming error inside the pipeline raises (:class:`VerifiedRetrievalError`) — the D-9
  "fail loudly for our bugs, degrade honestly for the world's" split.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

VerifiedStatus = Literal[
    "corroborated",  # the same answer on >= 2 independent sources
    "single_source",  # exactly one source has it — flagged, not hidden
    "conflicting",  # sources disagree — both surfaced, none silently chosen
    "not_found",  # no source has it — an honest zero, answer is None
    "error",  # every source failed — honest, never a fabricated answer
]


class SourceRef(BaseModel):
    """Provenance for one source that fed the result. Always travels with the answer so
    the engine can say "according to X" and the trace shows where it came from."""

    url: str
    domain: str
    published_date: str | None = None  # ISO-8601 when extractable, else None (unknown)
    snippet: str = ""


class Recency(BaseModel):
    """Recency is as important as content for a companion (brief §5). ``is_stale`` is only
    meaningful when ``is_time_sensitive`` — an old page about an old event is fine."""

    most_recent_source_date: str | None = None  # ISO-8601 of the freshest corroborating source
    is_time_sensitive: bool = False
    is_stale: bool = False


class ConflictClaim(BaseModel):
    """One side of a disagreement, surfaced when ``status == "conflicting"``."""

    source: str  # domain or url of the source making this claim
    claim: str


class SourceError(BaseModel):
    """A non-fatal per-source failure (404 / timeout / bot-wall / thin content). The
    pipeline notes it and proceeds on the other sources."""

    url: str
    reason: str


class Timings(BaseModel):
    """Per-stage timings so the latency budget is measured, not guessed (brief §6). Every
    latency claim is N>=5, median + p95 — a single sample measures noise."""

    search_ms: float = 0.0
    fetch_ms: float = 0.0
    extract_ms: float = 0.0
    total_ms: float = 0.0


class VerifiedResult(BaseModel):
    """Query in → this out. All three cardinalities the brief names (single / multiple /
    zero) plus conflict and error are first-class, explicit ``status`` values."""

    status: VerifiedStatus
    answer: str | None = None  # the grounded answer, or None for not_found / error
    confidence: float = 0.0  # derived from corroboration + recency, in [0, 1]
    sources: list[SourceRef] = Field(default_factory=list)  # provenance, always present
    corroboration_count: int = 0  # how many INDEPENDENT sources agreed (domains, de-duped)
    recency: Recency = Field(default_factory=Recency)
    conflict: list[ConflictClaim] | None = None  # populated iff status == "conflicting"
    formatted_voice: str = ""  # what the engine speaks (grounded; no added facts)
    formatted_json: dict[str, Any] | None = None  # structured fields when the caller asked
    timings: Timings = Field(default_factory=Timings)
    errors: list[SourceError] = Field(default_factory=list)  # per-source failures, non-fatal


class VerifiedRetrievalError(Exception):
    """A programming error INSIDE the pipeline — it must fail loudly (the D-9 / core.errors
    ethos). Dependency and source failures do NOT raise: they degrade to ``status="error"``
    or a per-source :class:`SourceError`. Never blanket-`except` this away."""


@runtime_checkable
class RetrievalPort(Protocol):
    """Verified retrieval behind the ports boundary. Query in → :class:`VerifiedResult` out.

    Contract:
    - NEVER raises for source/dependency failures — those become ``status="error"`` or
      per-source ``errors``. Only a :class:`VerifiedRetrievalError` (our bug) propagates.
    - Assumes a RESOLVED query (entity resolution is the engine's job upstream).
    - Does NOT block the live turn: callers run it on the background/waiter path by default
      (brief §6). ``budget_ms`` lets a caller cap a fast-path inline attempt and get back
      whatever is verified so far, marked partial, if it overruns.
    """

    async def verify(
        self,
        query: str,
        *,
        time_sensitive: bool | None = None,  # None → the adapter infers from the query
        want_json: bool = False,  # fill formatted_json with structured fields
        max_sources: int = 3,  # adaptive: start at 2, widen to this if not corroborated
        budget_ms: int | None = None,  # cap for a fast-path attempt; None → no cap
    ) -> VerifiedResult: ...
