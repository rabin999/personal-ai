"""Unit tests for Procedural Memory (spec §7) — DocStore faked."""

import pytest

from core.memory.procedural import INJECTION_THRESHOLD, ProceduralMemory
from tests.fakes import FakeDocStore


@pytest.fixture
def memory() -> ProceduralMemory:
    return ProceduralMemory(FakeDocStore())


async def _add_win_rule(memory: ProceduralMemory, user_id: str = "u_demo_001") -> str:
    rule = await memory.add_candidate(
        user_id,
        rule_text="when user says they need a win, offer one concrete small task",
        trigger="need a win",
        action="offer a concrete task",
    )
    return rule.id


# Acceptance 1: 5x consistent reinforcement crosses the threshold; a
# contradicted rule drops back below it.
async def test_five_consistent_reinforcements_cross_injection_threshold(
    memory: ProceduralMemory,
) -> None:
    rule_id = await _add_win_rule(memory)
    assert await memory.rules_for("u_demo_001") == []  # candidate not injectable

    for _ in range(5):
        rule = await memory.reinforce("u_demo_001", rule_id)

    assert rule.confidence >= INJECTION_THRESHOLD
    assert [r.id for r in await memory.rules_for("u_demo_001")] == [rule_id]


async def test_contradictions_demote_a_promoted_rule(memory: ProceduralMemory) -> None:
    rule_id = await _add_win_rule(memory)
    for _ in range(5):
        await memory.reinforce("u_demo_001", rule_id)
    assert await memory.rules_for("u_demo_001") != []

    for _ in range(3):
        await memory.reinforce("u_demo_001", rule_id, delta=-0.15)

    assert await memory.rules_for("u_demo_001") == []


# Acceptance 2: rules_for returns only above-threshold rules.
async def test_rules_for_filters_below_threshold_and_sorts_by_confidence(
    memory: ProceduralMemory,
) -> None:
    strong_id = await _add_win_rule(memory)
    weak = await memory.add_candidate(
        "u_demo_001",
        rule_text="when user mentions deadlines, be brief",
        trigger="deadline",
        action="be brief",
    )
    for _ in range(6):
        await memory.reinforce("u_demo_001", strong_id)

    rules = await memory.rules_for("u_demo_001")

    assert [r.id for r in rules] == [strong_id]
    assert weak.id not in [r.id for r in rules]


async def test_context_filter_matches_trigger_words(memory: ProceduralMemory) -> None:
    rule_id = await _add_win_rule(memory)
    for _ in range(5):
        await memory.reinforce("u_demo_001", rule_id)

    matching = await memory.rules_for("u_demo_001", context="I really need a win today")
    non_matching = await memory.rules_for("u_demo_001", context="what's the weather like")

    assert [r.id for r in matching] == [rule_id]
    assert non_matching == []


async def test_confidence_clamped_to_unit_interval(memory: ProceduralMemory) -> None:
    rule_id = await _add_win_rule(memory)
    for _ in range(20):
        rule = await memory.reinforce("u_demo_001", rule_id, delta=0.2)
    assert rule.confidence == 1.0
    for _ in range(20):
        rule = await memory.reinforce("u_demo_001", rule_id, delta=-0.3)
    assert rule.confidence == 0.0


async def test_reinforce_is_user_scoped(memory: ProceduralMemory) -> None:
    rule_id = await _add_win_rule(memory, "u_demo_001")
    with pytest.raises(KeyError):
        await memory.reinforce("u_demo_002", rule_id)


async def test_rules_never_leak_across_users(memory: ProceduralMemory) -> None:
    rule_id = await _add_win_rule(memory, "u_demo_001")
    for _ in range(5):
        await memory.reinforce("u_demo_001", rule_id)
    assert await memory.rules_for("u_demo_002") == []
