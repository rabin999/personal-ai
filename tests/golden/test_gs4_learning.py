"""GS4 — Learning & Adaptation golden set runner (spec §17/§18/§7).

Deterministic mechanism checks against real Mongo:
- a preference reinforced enough crosses the injection threshold → injectable;
- a contradicted OCEAN belief has confidence LOWERED, not deleted;
- a single correlation is a candidate, NOT confirmed until it clears the gate;
- no diagnostic/clinical language ever appears in the prompt rendering.

Superseded-fact-marked-not-deleted is asserted at the Fact-model contract level
(Graphiti sets valid_to live; see semantic integration tests).
"""

import uuid

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from core.memory.procedural import ProceduralMemory
from core.psych.consolidation import CORRELATION_CONFIRM_THRESHOLD, Consolidator
from core.psych.user_model import PsychUserModel, describe_for_prompt
from ports.graph_store import Fact

pytestmark = pytest.mark.integration

_CLINICAL = ["depress", "disorder", "diagnos", "anxiety disorder", "bipolar", "ptsd", "patholog"]


@pytest.fixture
def user() -> str:
    return f"gs4_{uuid.uuid4().hex[:10]}"


async def test_repeated_preference_crosses_injection_threshold(db: Database, user: str) -> None:
    proc = ProceduralMemory(MongoDocStore(db))
    rule = await proc.add_candidate(
        user,
        rule_text="user prefers blunt, direct feedback",
        trigger="feedback",
        action="be_direct",
    )
    # A fresh candidate is below the injection threshold → not yet retrievable.
    assert not any(r.id == rule.id for r in await proc.rules_for(user, context="feedback"))
    for _ in range(5):  # consistently reinforced across sessions
        rule = await proc.reinforce(user, rule.id)
    injected = await proc.rules_for(user, context="feedback")
    assert any(r.id == rule.id for r in injected), "reinforced rule never crossed threshold"
    await db.mongo("procedural_memory").delete_many({"user_id": user})


async def test_contradicted_belief_is_lowered_not_deleted(db: Database, user: str) -> None:
    psych = PsychUserModel(MongoDocStore(db))
    for _ in range(6):  # build confidence with consistent evidence
        await psych.update_trait(user, "conscientiousness", 0.9)
    high = (await psych.get(user)).ocean["conscientiousness"].confidence
    assert high > 0.2
    await psych.update_trait(user, "conscientiousness", 0.1)  # contradicting evidence
    model = await psych.get(user)
    lowered = model.ocean["conscientiousness"].confidence
    assert lowered < high, "contradiction did not lower confidence"
    assert "conscientiousness" in model.ocean, "belief was deleted instead of lowered"
    await db.mongo("psych_model").delete_many({"_id": user})


async def test_single_correlation_is_candidate_not_confirmed(db: Database, user: str) -> None:
    docs = MongoDocStore(db)
    consolidator = Consolidator(
        semantic=None,  # type: ignore[arg-type]
        procedural=None,  # type: ignore[arg-type]
        psych=None,  # type: ignore[arg-type]
        docs=docs,
        llm=None,  # type: ignore[arg-type]
    )
    await consolidator._record_correlation(
        user, "late_nights~low_mood", "stays up late then lower mood"
    )
    assert await consolidator.confirmed_correlations(user) == [], (
        "a single-sighting correlation was surfaced as confirmed (must gate at "
        f"{CORRELATION_CONFIRM_THRESHOLD} sightings)"
    )
    for _ in range(CORRELATION_CONFIRM_THRESHOLD):
        await consolidator._record_correlation(
            user, "late_nights~low_mood", "stays up late then lower mood"
        )
    assert await consolidator.confirmed_correlations(user), "correlation never confirmed after gate"
    await db.mongo("psych_correlations").delete_many({"user_id": user})


async def test_no_diagnostic_language_in_prompt_rendering(db: Database, user: str) -> None:
    psych = PsychUserModel(MongoDocStore(db))
    for _ in range(8):
        await psych.update_trait(user, "neuroticism", 0.95)
        await psych.update_mood(user, valence=-0.8, arousal=-0.4)
    rendered = describe_for_prompt(await psych.get(user)).lower()
    for term in _CLINICAL:
        assert term not in rendered, f"clinical/diagnostic term leaked into prompt: {term!r}"
    await db.mongo("psych_model").delete_many({"_id": user})


def test_superseded_fact_marked_not_deleted() -> None:
    """Fact contract: a superseded fact carries valid_to and is not current,
    but is still present (never deleted) — §6/§18 temporal validity."""
    current = Fact(fact="user works at Acme", valid_from="2026-01-01", valid_to=None)
    superseded = Fact(fact="user worked at Beta", valid_from="2025-01-01", valid_to="2026-01-01")
    assert current.is_current and not superseded.is_current
    assert superseded.fact and superseded.valid_to  # retained, not deleted
