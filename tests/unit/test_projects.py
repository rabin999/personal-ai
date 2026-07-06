"""Unit tests for Projects (spec §16) — ports faked, real core modules."""

from pathlib import Path

import pytest

from core.memory.entities import EntityResolver
from core.profile import ProfileService, TraitRegistry
from core.projects.service import ProjectNotFound, ProjectService
from core.tools.registry import ToolContext, ToolRegistry
from tests.fakes import FakeDocStore, FakeVectorStore

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"
USER = "u_demo_001"


def _trade(ticker: str, side: str, qty: float, price: float) -> dict[str, object]:
    return {"ticker": ticker, "side": side, "qty": qty, "price": price}


class Harness:
    def __init__(self) -> None:
        self.docs = FakeDocStore()
        self.vectors = FakeVectorStore()
        self.registry = ToolRegistry()
        self.entities = EntityResolver(self.vectors)
        self.service = ProjectService(self.docs, self.entities, self.registry, llm=None)

    async def seed(self) -> "Harness":
        profiles = ProfileService(self.docs)
        await TraitRegistry(self.docs, profiles).seed_defaults(DEFAULTS_DIR)
        return self


@pytest.fixture
async def h() -> Harness:
    return await Harness().seed()


# Acceptance: creating a finance project registers its log_entry action and
# an entity pointer.
async def test_create_registers_action_tool_and_entity_pointer(h: Harness) -> None:
    project = await h.service.create(USER, "finance_portfolio", "NEPSE Portfolio")

    tool_ids = [t.id for t in h.registry.tools_for_context("finance_portfolio")]
    assert "finance_portfolio.log_entry" in tool_ids
    spec, _ = h.registry.get("finance_portfolio.log_entry")
    assert spec.requires_confirmation and spec.type == "action"
    assert not spec.interruptible  # writes are barge-in safe (§13 rule 6)

    candidates = await h.entities.resolve(USER, "my stock portfolio tracking")
    assert candidates and candidates[0].entity_id == project.id


async def test_type_tools_absent_until_an_instance_exists(h: Harness) -> None:
    assert h.registry.tools_for_context("finance_portfolio") == []
    await h.service.create(USER, "finance_portfolio", "P")
    assert h.registry.tools_for_context("finance_portfolio") != []


async def test_create_with_unknown_type_fails(h: Harness) -> None:
    with pytest.raises(ProjectNotFound):
        await h.service.create(USER, "spaceship_program", "X")


# Acceptance: logging a sell computes updated P&L from the ledger.
async def test_sell_computes_realized_pnl_with_average_cost(h: Harness) -> None:
    project = await h.service.create(USER, "finance_portfolio", "P")
    await h.service.log_entry(project.id, USER, _trade("SYPNL", "buy", 10, 40))
    await h.service.log_entry(project.id, USER, _trade("SYPNL", "buy", 10, 50))
    await h.service.log_entry(project.id, USER, _trade("SYPNL", "sell", 5, 60))

    state = await h.service.state(project.id, USER)

    # avg cost = 45; sell 5 @ 60 → realized 75; 15 shares remain.
    assert state.metrics["realized_pnl"] == pytest.approx(75.0)
    assert state.metrics["positions"]["SYPNL"]["qty"] == 15
    assert state.metrics["positions"]["SYPNL"]["avg_cost"] == pytest.approx(45.0)
    assert state.metrics["net_invested"] == pytest.approx(10 * 40 + 10 * 50 - 5 * 60)


# Acceptance: projects.state returns metrics + recent entries for §10.
async def test_state_and_project_context_feed_prompt_assembly(h: Harness) -> None:
    project = await h.service.create(USER, "finance_portfolio", "NEPSE Portfolio")
    await h.service.log_entry(project.id, USER, _trade("SYPNL", "buy", 20, 42))

    state = await h.service.state(project.id, USER)
    assert len(state.recent_entries) == 1

    context = await h.service.project_context(USER, project.id)
    assert context is not None
    assert "NEPSE Portfolio" in context and "SYPNL" in context


# Acceptance: an insight is stored pending and only spoken after consent.
async def test_insight_pending_until_consent_then_delivered_with_caveat(h: Harness) -> None:
    project = await h.service.create(USER, "finance_portfolio", "P")
    await h.service.log_entry(project.id, USER, _trade("SYPNL", "buy", 5, 40))

    insight = await h.service.run_insight(project.id, USER)
    assert insight is not None and insight.status == "pending"

    pending = await h.service.pending_insight(project.id, USER)
    assert pending is not None and pending.id == insight.id

    spoken = await h.service.consent_and_deliver(insight.id, USER)
    assert "not a financial advisor" in spoken
    assert await h.service.pending_insight(project.id, USER) is None  # no longer pending


async def test_dismissed_insight_is_never_delivered(h: Harness) -> None:
    project = await h.service.create(USER, "finance_portfolio", "P")
    await h.service.log_entry(project.id, USER, _trade("A", "buy", 1, 1))
    insight = await h.service.run_insight(project.id, USER)
    assert insight is not None

    await h.service.dismiss_insight(insight.id, USER)
    assert await h.service.pending_insight(project.id, USER) is None


async def test_action_tool_handler_logs_entry_and_triggers_insight(h: Harness) -> None:
    project = await h.service.create(USER, "finance_portfolio", "P")
    _, handler = h.registry.get("finance_portfolio.log_entry")

    output = await handler(
        _trade("SYPNL", "buy", 5, 40),
        ToolContext(
            user_id=USER, session_id="s1", project_id=project.id, project_type="finance_portfolio"
        ),
    )

    assert output["logged"]["ticker"] == "SYPNL"
    assert output["pending_insight"] is not None
    state = await h.service.state(project.id, USER)
    assert state.metrics["positions"]["SYPNL"]["qty"] == 5


async def test_rename_updates_entity_pointer_in_place(h: Harness) -> None:
    project = await h.service.create(USER, "finance_portfolio", "NEPSE Portfolio")
    await h.service.rename(project.id, USER, "Sherpa Capital")

    candidates = await h.entities.resolve(USER, "Sherpa Capital")
    assert candidates and candidates[0].entity_id == project.id
    assert candidates[0].name == "Sherpa Capital"


async def test_two_user_isolation_on_projects_and_insights(h: Harness) -> None:
    project = await h.service.create(USER, "finance_portfolio", "P")
    await h.service.log_entry(project.id, USER, _trade("A", "buy", 1, 1))
    insight = await h.service.run_insight(project.id, USER)
    assert insight is not None

    other = "u_demo_002"
    with pytest.raises(ProjectNotFound):
        await h.service.log_entry(project.id, other, _trade("B", "buy", 1, 1))
    with pytest.raises(ProjectNotFound):
        await h.service.state(project.id, other)
    with pytest.raises(ProjectNotFound):
        await h.service.consent_and_deliver(insight.id, other)
    assert await h.service.project_context(other, project.id) is None
