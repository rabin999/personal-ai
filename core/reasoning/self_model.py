"""Self-Model / metacognition (spec §9): the system's view of its own turns.

Tracks per-turn confidence and facts used (Mongo ``self_model_log``), keeps
the companion's own prior statements searchable for consistency ("last time
I suggested X"), and catches drafts that overclaim feeling or consciousness
before they reach the user, rewriting them to a validating-but-honest form.

This is a *functional* self-model only — never labeled or surfaced as
consciousness (rule 3).
"""

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ports.doc_store import DocStore
from ports.llm import LLM, LLMUnavailable
from ports.vector_store import VectorDoc, VectorStore

logger = logging.getLogger(__name__)

SELF_MODEL_LOG_COLLECTION = "self_model_log"
SELF_STATEMENTS_COLLECTION = "self_statements"

BoundaryFlag = Literal["overclaim_empathy", "overclaim_consciousness"] | None

# Heuristic overclaim patterns — a backstop when the generation judgment
# misses; wording-level tuning is a human pass (contract §7).
_OVERCLAIM_PATTERNS: list[tuple[str, str]] = [
    (r"\bi (?:understand|know) exactly (?:how|what) you feel\b", "overclaim_empathy"),
    (r"\bi (?:truly |really )?feel your (?:pain|sadness|grief)\b", "overclaim_empathy"),
    (
        r"\bi feel (?:so |really |truly )?(?:sad|happy|hurt|lonely|excited) (?:for|with) you\b",
        "overclaim_empathy",
    ),
    (r"\bas a conscious being\b", "overclaim_consciousness"),
    (r"\bi am (?:truly |really )?conscious\b", "overclaim_consciousness"),
    (r"\bi (?:have|possess) (?:real |genuine )?feelings\b", "overclaim_consciousness"),
]

_REWRITE_INSTRUCTIONS = (
    "You edit one line of an AI companion's draft reply. The draft overclaims "
    "felt emotion or consciousness. Rewrite it to be validating but honest — "
    "acknowledge the person's experience warmly without claiming the AI feels "
    "or is conscious (e.g. 'that sounds really hard'). Keep everything else, "
    "the tone, and any [tags] intact. Return ONLY the rewritten reply text."
)

# Safe fallback when the rewrite model is unavailable (retry-then-fallback
# rule §0.5): drop the overclaiming sentence's claim by generic validation.
_FALLBACK_REWRITE = "That sounds really hard, and I'm here with you."


class TurnRecord(BaseModel):
    turn_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    facts_used: list[str] = Field(default_factory=list)
    novel_claim: bool = False
    capability_boundary_flag: BoundaryFlag = None
    self_reference: list[str] = Field(default_factory=list)


class PriorStatement(BaseModel):
    text: str
    turn_id: str | None = None
    timestamp: str | None = None
    score: float


class BoundaryCheck(BaseModel):
    flagged: bool
    flag: BoundaryFlag = None
    rewritten_text: str | None = None


class SelfModel:
    def __init__(self, docs: DocStore, vectors: VectorStore, llm: LLM | None = None) -> None:
        self._docs = docs
        self._vectors = vectors
        self._llm = llm

    async def log(self, record: TurnRecord, statement_text: str | None = None) -> None:
        """Persist the turn record; index the spoken statement for recall."""
        doc: dict[str, Any] = record.model_dump()
        doc["_id"] = doc.pop("turn_id")
        await self._docs.put(SELF_MODEL_LOG_COLLECTION, record.turn_id, doc)
        if statement_text:
            await self._vectors.upsert_texts(
                SELF_STATEMENTS_COLLECTION,
                [
                    VectorDoc(
                        id=record.turn_id,
                        text=statement_text,
                        payload={
                            "user_id": record.user_id,
                            "turn_id": record.turn_id,
                            "timestamp": record.timestamp,
                        },
                    )
                ],
            )

    async def recall(self, user_id: str, query: str, k: int = 3) -> list[PriorStatement]:
        """Own prior statements relevant to the query, this user's sessions only."""
        hits = await self._vectors.hybrid_search(
            SELF_STATEMENTS_COLLECTION, query, user_id=user_id, k=k
        )
        return [
            PriorStatement(
                text=str(hit.payload.get("text", "")),
                turn_id=hit.payload.get("turn_id"),
                timestamp=hit.payload.get("timestamp"),
                score=hit.score,
            )
            for hit in hits
        ]

    async def check_boundary(
        self,
        user_id: str,
        draft_text: str,
        judgment_flag: BoundaryFlag = None,
    ) -> BoundaryCheck:
        """Flag and rewrite overclaiming drafts before they reach TTS (rule 1)."""
        flag = judgment_flag or _scan_for_overclaim(draft_text)
        if flag is None:
            return BoundaryCheck(flagged=False)
        rewritten = await self._rewrite(user_id, draft_text)
        return BoundaryCheck(flagged=True, flag=flag, rewritten_text=rewritten)

    async def _rewrite(self, user_id: str, draft_text: str) -> str:
        if self._llm is None:
            return _FALLBACK_REWRITE
        messages = [
            {"role": "system", "content": _REWRITE_INSTRUCTIONS},
            {"role": "user", "content": draft_text},
        ]
        for _ in range(2):  # retry once (rule §0.5)
            try:
                result = await self._llm.complete(user_id, messages, "simple", purpose="self_model")
            except LLMUnavailable:
                continue
            if result.text.strip():
                return result.text.strip()
        logger.warning("overclaim rewrite failed twice; using safe fallback")
        return _FALLBACK_REWRITE


def _scan_for_overclaim(text: str) -> BoundaryFlag:
    lowered = text.lower()
    for pattern, flag in _OVERCLAIM_PATTERNS:
        if re.search(pattern, lowered):
            return "overclaim_empathy" if flag == "overclaim_empathy" else "overclaim_consciousness"
    return None
