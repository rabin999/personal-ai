"""S4 EXTRACT + per-source claim extraction (feeds S6 cross-check).

Two jobs:

- **thin-reject** (S4): a page whose relevance-filtered text is under the word-count
  threshold is blocked / paywall-teaser / JS-never-settled — reject it as a source, do
  not trust a truncated fact.
- **claim extraction**: pull the ONE short answer each page gives for the query, so
  cross-check can see whether independent domains agree. Deterministic first for the
  things that must not be paraphrased — numbers, prices, dates, plain names — then a
  small fast LLM for everything else (brief §6). The LLM here is an EXTRACTOR constrained
  to the page text: it may return ``NONE`` (topic-match without an answer-match is NOT a
  claim) and must not invent.

The :class:`AnswerExtractor` protocol keeps the LLM behind an interface so the harness
can drive the whole pipeline deterministically with a scripted extractor, while the real
adapter uses the model.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

from pydantic import BaseModel, ValidationError

from adapters.retrieval.fetch import FetchedPage
from ports.llm import LLM, LLMUnavailable

logger = logging.getLogger(__name__)


class ExtractedClaim(BaseModel):
    """The answer one source gives for the query, plus a normalized key for de-dupe."""

    domain: str
    url: str
    answer: str  # the human-readable claim ("Sushila Karki", "$1,240", "39 rupees")
    kind: str = "text"  # "number" | "date" | "text" — how it was normalized
    text: str = ""  # page-text snippet, for cross-domain syndication de-dupe (S6)


def word_count(text: str) -> int:
    return len(text.split())


def is_thin(page: FetchedPage, threshold: int) -> bool:
    """True when the page has too little real content to trust (S4 thin-reject)."""
    return word_count(page.best_text()) < threshold


class AnswerExtractor(Protocol):
    async def extract(self, query: str, page: FetchedPage) -> ExtractedClaim | None: ...


# ── Deterministic extraction ─────────────────────────────────────────────────
# A query asking for a number/price/amount → pull the number verbatim, don't paraphrase.
_NUMERIC_QUERY = re.compile(
    r"\b(price|ltp|cost|rate|worth|how much|how many|population|number of|"
    r"percentage|percent|share price|market cap|value)\b",
    re.IGNORECASE,
)
# Money or bare number, optionally with a currency symbol/word and thousands separators.
_NUMBER_RE = re.compile(
    r"(?:(?:Rs\.?|NPR|USD|\$|₹|€|£)\s?)?"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?:%|percent|rupees|dollars|npr|usd))?",
    re.IGNORECASE,
)


def looks_numeric(query: str) -> bool:
    return bool(_NUMERIC_QUERY.search(query))


def normalize_number(answer: str) -> str | None:
    """Reduce a numeric claim to a canonical key (drop separators/currency/units)."""
    m = _NUMBER_RE.search(answer)
    if not m:
        return None
    return m.group(1).replace(",", "").rstrip("0").rstrip(".") or "0"


def extract_number_from_text(text: str) -> str | None:
    """First salient number in the page text (deterministic price/amount extraction)."""
    m = _NUMBER_RE.search(text)
    return m.group(0).strip() if m else None


class DeterministicThenLLMExtractor:
    """Numbers/prices deterministically; everything else via a small fast LLM."""

    def __init__(
        self, llm: LLM, user_id: str, model: str, temperature: float, session_id: str | None = None
    ) -> None:
        self._llm = llm
        self._user_id = user_id
        self._model = model
        self._temperature = temperature
        self._session_id = session_id

    async def extract(self, query: str, page: FetchedPage) -> ExtractedClaim | None:
        text = page.best_text()
        if not text:
            return None
        if looks_numeric(query):
            num = extract_number_from_text(text[:2000])
            if num is not None:
                return ExtractedClaim(
                    domain=page.domain,
                    url=page.final_url,
                    answer=num,
                    kind="number",
                    text=text[:400],
                )
            # No number on a numeric query = topic without answer; fall through to LLM.
        return await self._llm_extract(query, page, text)

    async def _llm_extract(self, query: str, page: FetchedPage, text: str) -> ExtractedClaim | None:
        prompt = (
            "You are a strict extractor, NOT a researcher. From the PAGE TEXT below, "
            "extract the single short answer to the QUESTION, using ONLY what the text "
            "actually states. If the page does not clearly answer it, output exactly "
            "NONE. Never guess. Answer in the fewest words (a name, phrase, or number).\n\n"
            f"QUESTION: {query}\n\nPAGE TEXT:\n{text[:6000]}\n\nANSWER:"
        )
        try:
            result = await self._llm.complete(
                self._user_id,
                [{"role": "user", "content": prompt}],
                "simple",
                model=self._model,
                temperature=self._temperature,
                session_id=self._session_id,
                max_tokens=40,
                purpose="retrieval_extract",
            )
        except LLMUnavailable:
            logger.warning("extractor LLM unavailable for %s", page.domain)
            return None
        answer = result.text.strip().strip("\"'")
        if not answer or answer.upper().startswith("NONE"):
            return None
        return ExtractedClaim(
            domain=page.domain, url=page.final_url, answer=answer, kind="text", text=text[:400]
        )


class ClaimModel(BaseModel):
    """Validation shell for a future JSON-mode extractor (invariant 5)."""

    answer: str | None = None


def parse_claim_json(raw: str) -> str | None:
    try:
        return ClaimModel.model_validate_json(raw).answer
    except ValidationError:
        return None
