"""Unit tests for Consolidation (spec §18) — LLM scripted, stores faked."""

import json

import pytest

from core.memory.procedural import ProceduralMemory
from core.memory.semantic import SemanticMemory
from core.memory.working import Turn
from core.psych.consolidation import (
    CORRELATIONS_COLLECTION,
    Consolidator,
)
from core.psych.user_model import PsychUserModel
from tests.fakes import FakeDocStore, FakeGraphStore, FakeLLM

USER = "u_demo_001"


def _analysis(
    observations: list[dict[str, str]] | None = None,
    topic: str | None = None,
    valence: float = 0.0,
    ocean: dict[str, float] | None = None,
) -> str:
    return json.dumps(
        {
            "behavior_observations": observations or [],
            "dominant_topic": topic,
            "session_valence": valence,
            "session_arousal": 0.0,
            "ocean_evidence": ocean or {},
        }
    )


def _turns(*texts: str) -> list[Turn]:
    return [Turn(role="user", text=t) for t in texts]


class Harness:
    def __init__(self, llm_responses: list[str]) -> None:
        self.docs = FakeDocStore()
        self.graph = FakeGraphStore()
        self.llm = FakeLLM(llm_responses)
        self.procedural = ProceduralMemory(self.docs)
        self.psych = PsychUserModel(self.docs)
        self.consolidator = Consolidator(
            SemanticMemory(self.graph), self.procedural, self.psych, self.docs, self.llm
        )


WIN_OBSERVATION = {
    "rule_text": "when user says they need a win, offer one small concrete task",
    "trigger": "need a win",
    "action": "offer a concrete task",
    "evidence": "confirming",
}


# Acceptance: session facts reach semantic memory.
async def test_transcript_feeds_semantic_extraction() -> None:
    h = Harness([_analysis()])
    report = await h.consolidator.consolidate(USER, "s1", _turns("my cat is called Waffles"))
    assert report.facts_extracted
    assert h.graph.episodes[0]["user_id"] == USER
    assert "Waffles" in h.graph.episodes[0]["text"]


# Acceptance: repeated behavior across sessions crosses the injection threshold.
async def test_repeated_pattern_across_sessions_promotes_rule() -> None:
    responses = [_analysis([WIN_OBSERVATION]) for _ in range(6)]
    h = Harness(responses)

    for i in range(6):
        await h.consolidator.consolidate(USER, f"s{i}", _turns("I need a win today"))

    rules = await h.procedural.rules_for(USER)
    assert len(rules) == 1  # matched and reinforced, never duplicated
    assert rules[0].confidence >= 0.6
    assert rules[0].evidence_count >= 6


# Acceptance: contradicted belief loses confidence, not ignored.
async def test_contradiction_lowers_rule_confidence() -> None:
    contradiction = {**WIN_OBSERVATION, "evidence": "contradicting"}
    h = Harness(
        [_analysis([WIN_OBSERVATION]) for _ in range(6)]
        + [_analysis([contradiction]) for _ in range(2)]
    )
    for i in range(6):
        await h.consolidator.consolidate(USER, f"s{i}", _turns("I need a win"))
    promoted = (await h.procedural.rules_for(USER))[0]

    for i in range(2):
        await h.consolidator.consolidate(USER, f"sc{i}", _turns("actually that annoys me"))

    docs = await h.docs.find("procedural", {"user_id": USER})
    assert float(docs[0]["confidence"]) < promoted.confidence


# Acceptance: a single correlation is a candidate, not acted upon.
async def test_single_correlation_stays_candidate_until_confirmed() -> None:
    low_mood_session = _analysis(topic="work deadlines", valence=-0.6)
    h = Harness([low_mood_session] * 4)
    # Build a baseline first so "below usual" is detectable.
    for _ in range(4):
        await h.psych.update_mood(USER, valence=0.4, arousal=0.0)

    await h.consolidator.consolidate(USER, "s1", _turns("work is crushing me"))
    rows = await h.docs.find(CORRELATIONS_COLLECTION, {"user_id": USER})
    assert len(rows) == 1
    assert rows[0]["status"] == "candidate"
    assert await h.consolidator.confirmed_correlations(USER) == []

    # Repeated sightings cross the confirmation gate.
    await h.consolidator.consolidate(USER, "s2", _turns("work again, exhausted"))
    await h.consolidator.consolidate(USER, "s3", _turns("deadlines never end"))
    confirmed = await h.consolidator.confirmed_correlations(USER)
    assert len(confirmed) == 1
    assert "correlation, not causation" in confirmed[0]["description"]


async def test_mood_and_traits_update_from_session() -> None:
    h = Harness([_analysis(valence=-0.4, ocean={"conscientiousness": 0.8})])
    turns = [
        Turn(role="user", text="rough day", emotion={"valence": -0.5, "arousal": 0.2}),
        Turn(role="user", text="really rough", emotion={"valence": -0.3, "arousal": 0.4}),
    ]

    report = await h.consolidator.consolidate(USER, "s1", turns)

    assert report.mood_updated and report.traits_updated == 1
    model = await h.psych.get(USER)
    assert model.mood_baseline.valence == pytest.approx(-0.4)  # measured turns win
    assert model.ocean["conscientiousness"].value > 0.5


async def test_failed_analysis_still_extracts_facts() -> None:
    h = Harness(["garbage", "more garbage"])
    report = await h.consolidator.consolidate(USER, "s1", _turns("my cat is Waffles"))
    assert report.facts_extracted
    assert report.rules_added == 0 and not report.mood_updated
    assert len(h.llm.calls) == 2  # retried once, then degraded gracefully


async def test_empty_transcript_is_a_no_op() -> None:
    h = Harness([])
    report = await h.consolidator.consolidate(USER, "s1", [])
    assert not report.facts_extracted and h.graph.episodes == []


async def test_task_handler_consolidates_from_queue_payload() -> None:
    from ports.queue import QueuedTask

    h = Harness([_analysis()])
    handler = h.consolidator.task_handler()
    task = QueuedTask(
        task_id="t1",
        session_id="s1",
        user_id=USER,
        type="consolidation",
        params={"transcript": [{"role": "user", "text": "my cat is Waffles"}]},
        created_at="2026-07-06T10:00:00+00:00",
    )
    result = await handler(task)
    assert result["facts_extracted"] is True
    assert h.graph.episodes[0]["user_id"] == USER
