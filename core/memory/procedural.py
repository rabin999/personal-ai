"""Procedural Memory (spec §7): learned behavioral rules with confidence.

Rules ("when the user says 'need a win', offer a concrete task") enter as
low-confidence candidates, gain or lose confidence through reinforcement
(Consolidation §18), and are only surfaced to Prompt Assembly (§10 step 7)
once they cross the injection threshold.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from ports.doc_store import DocStore

PROCEDURAL_COLLECTION = "procedural"

# Candidates start well below the injection threshold: one confirmation is
# not a pattern. ~4-5 consistent reinforcements at the default delta (0.08)
# promote a rule; contradictions push it back out.
INITIAL_CONFIDENCE = 0.3
INJECTION_THRESHOLD = 0.6
DEFAULT_DELTA = 0.08


class Rule(BaseModel):
    id: str
    rule_text: str
    trigger: str
    action: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_count: int = 0
    updated_at: str


class ProceduralMemory:
    def __init__(self, docs: DocStore, injection_threshold: float = INJECTION_THRESHOLD) -> None:
        self._docs = docs
        self._threshold = injection_threshold

    async def add_candidate(
        self, user_id: str, *, rule_text: str, trigger: str, action: str
    ) -> Rule:
        """Store a new low-confidence rule (rule 1)."""
        rule = Rule(
            id=str(uuid.uuid4()),
            rule_text=rule_text,
            trigger=trigger,
            action=action,
            confidence=INITIAL_CONFIDENCE,
            evidence_count=1,
            updated_at=datetime.now(UTC).isoformat(),
        )
        await self._docs.put(PROCEDURAL_COLLECTION, rule.id, _to_doc(user_id, rule))
        return rule

    async def reinforce(self, user_id: str, rule_id: str, delta: float = DEFAULT_DELTA) -> Rule:
        """Shift confidence up (confirming) or down (contradicting evidence)."""
        doc = await self._docs.get(PROCEDURAL_COLLECTION, rule_id)
        if doc is None or doc.get("user_id") != user_id:
            raise KeyError(f"no rule {rule_id} for user {user_id}")
        rule = _from_doc(doc)
        rule.confidence = min(1.0, max(0.0, rule.confidence + delta))
        rule.evidence_count += 1
        rule.updated_at = datetime.now(UTC).isoformat()
        await self._docs.put(PROCEDURAL_COLLECTION, rule.id, _to_doc(user_id, rule))
        return rule

    async def rules_for(self, user_id: str, context: str | None = None) -> list[Rule]:
        """Above-threshold rules for this user, optionally filtered to a context (rule 2)."""
        docs = await self._docs.find(PROCEDURAL_COLLECTION, {"user_id": user_id})
        rules = [_from_doc(d) for d in docs]
        rules = [r for r in rules if r.confidence >= self._threshold]
        if context is not None:
            context_words = _words(context)
            rules = [r for r in rules if _words(r.trigger) & context_words]
        rules.sort(key=lambda r: r.confidence, reverse=True)
        return rules


def _words(text: str) -> set[str]:
    return {word for word in text.lower().split() if len(word) > 2}


def _to_doc(user_id: str, rule: Rule) -> dict[str, Any]:
    doc = rule.model_dump()
    doc["_id"] = doc.pop("id")
    doc["user_id"] = user_id
    return doc


def _from_doc(doc: dict[str, Any]) -> Rule:
    data = {k: v for k, v in doc.items() if k not in ("_id", "user_id")}
    return Rule.model_validate({"id": doc["_id"], **data})
