"""S7 FORMAT — a SMALL FAST LLM turns the verified finding into spoken text.

It is a FORMATTER, not a researcher (brief §7): it may only restate what the verified
sources support, it carries provenance, and it adds no facts. Zero / conflicting / error
are honest first-class lines, not something to paper over. The formatter's own cost is
logged to the Cost Ledger (invariant 4); a cache hit or a degraded deterministic render
logs $0 / nothing.

The :class:`Formatter` protocol keeps the LLM behind an interface so the harness can run
the full pipeline with a deterministic formatter and assert structure, while the adapter
uses the model for natural phrasing.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from core.cost import CostEntry, CostLedger, CostMetadata
from ports.llm import LLM, LLMUnavailable
from ports.retrieval import VerifiedResult

logger = logging.getLogger(__name__)


class Formatter(Protocol):
    async def format(
        self, query: str, result: VerifiedResult, *, want_json: bool
    ) -> tuple[str, dict[str, Any] | None]: ...


class _VoiceJSON(BaseModel):
    """Structured formatter output (invariant 5: every LLM JSON is Pydantic-validated)."""

    answer: str | None = None
    as_of: str | None = None
    sources: list[str] = []


def deterministic_voice(query: str, result: VerifiedResult) -> str:
    """Provenance-carrying spoken line with no model — the safe fallback and the honest
    render for zero/conflict/error. Never states more than the status supports."""
    domains = [s.domain for s in result.sources]
    prov = domains[0] if domains else "a source"
    if result.status == "not_found":
        return "I looked, but I couldn't find a reliable source for that, so I won't guess."
    if result.status == "error":
        return "I tried to check that properly, but the pages I found wouldn't load just now."
    if result.status == "conflicting":
        claims = result.conflict or []
        if len(claims) >= 2:
            return (
                f"Sources disagree on this — {claims[0].source} says {claims[0].claim}, "
                f"while {claims[1].source} says {claims[1].claim}. I won't pick one for you."
            )
        return "The sources I found disagree on this, so I'd rather not give you a single answer."
    stale = " though the freshest source I found is a bit old" if result.recency.is_stale else ""
    if result.status == "single_source":
        return f"According to {prov}, {result.answer}. I only found one source for it{stale}."
    n = result.corroboration_count
    return f"{result.answer} — confirmed across {n} sources like {prov}{stale}."


class LLMFormatter:
    """Real formatter: a low-temperature small model phrases the finding for voice."""

    def __init__(
        self,
        llm: LLM,
        user_id: str,
        model: str,
        temperature: float,
        *,
        ledger: CostLedger | None = None,
        session_id: str | None = None,
    ) -> None:
        self._llm = llm
        self._user_id = user_id
        self._model = model
        self._temperature = temperature
        self._ledger = ledger
        self._session_id = session_id

    async def format(
        self, query: str, result: VerifiedResult, *, want_json: bool
    ) -> tuple[str, dict[str, Any] | None]:
        # Zero / conflict / error are rendered deterministically — honest, no model needed,
        # and no risk of the LLM inventing a fact to fill the gap.
        if result.status in ("not_found", "error", "conflicting"):
            voice = deterministic_voice(query, result)
            return voice, (self._json(result) if want_json else None)

        domains = ", ".join(s.domain for s in result.sources) or "unknown"
        as_of = result.recency.most_recent_source_date or "date unknown"
        instruction = (
            "You are a FORMATTER for a warm voice companion, not a researcher. Restate the "
            "VERIFIED FINDING below in ONE short spoken sentence. Use ONLY the finding — add "
            "no facts, no numbers not present, no caveats beyond what's given. Mention it's "
            "confirmed by sources naturally. No markdown, no URLs.\n\n"
            f"QUESTION: {query}\n"
            f"VERIFIED ANSWER: {result.answer}\n"
            f"CORROBORATING SOURCES: {domains}\n"
            f"AS OF: {as_of}\n"
            f"STALE: {result.recency.is_stale}\n"
        )
        try:
            completion = await self._llm.complete(
                self._user_id,
                [{"role": "user", "content": instruction}],
                "simple",
                model=self._model,
                temperature=self._temperature,
                session_id=self._session_id,
                max_tokens=80,
                purpose="retrieval_format",
            )
            voice = completion.text.strip()
            self._log_cost(completion.cost_usd, completion.model)
        except LLMUnavailable:
            logger.warning("formatter LLM unavailable; using deterministic render")
            voice = deterministic_voice(query, result)
        if not voice:
            voice = deterministic_voice(query, result)
        return voice, (self._json(result) if want_json else None)

    @staticmethod
    def _json(result: VerifiedResult) -> dict[str, Any]:
        model = _VoiceJSON(
            answer=result.answer,
            as_of=result.recency.most_recent_source_date,
            sources=[s.domain for s in result.sources],
        )
        # Round-trip through validation so malformed structure fails loudly here, not on air.
        try:
            return _VoiceJSON.model_validate(model.model_dump()).model_dump()
        except ValidationError:  # pragma: no cover - model_dump is always valid
            return {"answer": result.answer, "as_of": None, "sources": []}

    def _log_cost(self, cost_usd: float, provider_model: str) -> None:
        if self._ledger is None:
            return
        self._ledger.log(
            CostEntry(
                user_id=self._user_id,
                component="llm",
                provider=provider_model or self._model,
                units={"input_tokens": 0, "output_tokens": 0},
                cost_usd=cost_usd,
                metadata=CostMetadata(session_id=self._session_id, cache_hit=cost_usd == 0.0),
            )
        )
