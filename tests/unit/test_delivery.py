"""Unit tests for pull-at-pause delivery (spec §14 rule 2) — queue and LLM faked."""

import json
from typing import Any

from core.tools.delivery import DeliveryComposer
from ports.queue import QueuedTask
from tests.fakes import FakeLLM


class FakeQueue:
    def __init__(self, tasks: list[QueuedTask]) -> None:
        self.tasks = tasks
        self.delivered: list[str] = []
        self.suppressed: list[str] = []

    async def pending_deliveries(self, session_id: str) -> list[QueuedTask]:
        return [t for t in self.tasks if t.session_id == session_id]

    async def pending_deliveries_for_user(
        self, user_id: str, *, exclude_session: str | None = None
    ) -> list[QueuedTask]:
        return [
            t
            for t in self.tasks
            if t.user_id == user_id
            and t.session_id != exclude_session
            and t.status == "completed"
            and t.delivery_state == "pending"
        ]

    async def mark_delivered(self, task_id: str) -> None:
        self.delivered.append(task_id)

    async def mark_suppressed(self, task_id: str) -> None:
        self.suppressed.append(task_id)

    # Unused protocol methods:
    async def enqueue(self, **kwargs: Any) -> str:
        raise NotImplementedError

    async def status(self, task_id: str) -> QueuedTask:
        raise NotImplementedError

    async def claim_next(self, timeout_s: float = 1.0) -> QueuedTask | None:
        raise NotImplementedError

    async def complete(self, task_id: str, result: dict[str, Any]) -> None:
        raise NotImplementedError

    async def fail(self, task_id: str, error: str) -> None:
        raise NotImplementedError


def _task(task_id: str = "t1") -> QueuedTask:
    return QueuedTask(
        task_id=task_id,
        session_id="s1",
        user_id="u_demo_001",
        type="web_search",
        params={"query": "SYPNL biotech news"},
        status="completed",
        result={"summary": "SYPNL announced trial results; stock up 12%"},
        created_at="2026-07-06T10:00:00+00:00",
    )


async def test_relevant_result_becomes_llm_composed_interjection() -> None:
    line = "oh — that SYPNL news came in: trial results out, stock jumped 12%"
    llm = FakeLLM([json.dumps({"relevant": True, "line": line})])
    queue = FakeQueue([_task()])
    composer = DeliveryComposer(queue, llm)

    interjections = await composer.deliveries_for_pause("s1", "u_demo_001", "user: anyway...")

    assert len(interjections) == 1
    assert "SYPNL" in interjections[0].line
    assert queue.delivered == ["t1"] and queue.suppressed == []
    # Composition is generative, not templated: the result payload went to the LLM.
    assert "trial results" in json.dumps(llm.calls[0]["messages"])


async def test_abandoned_topic_is_suppressed_not_spoken() -> None:
    llm = FakeLLM([json.dumps({"relevant": False, "line": ""})])
    queue = FakeQueue([_task()])
    composer = DeliveryComposer(queue, llm)

    interjections = await composer.deliveries_for_pause(
        "s1", "u_demo_001", "user: forget the stocks, my mom is in hospital"
    )

    assert interjections == []
    assert queue.suppressed == ["t1"] and queue.delivered == []


async def test_malformed_composition_retries_then_suppresses() -> None:
    llm = FakeLLM(["not json", "still not json"])
    queue = FakeQueue([_task()])
    composer = DeliveryComposer(queue, llm)

    interjections = await composer.deliveries_for_pause("s1", "u_demo_001", "...")

    assert interjections == []
    assert queue.suppressed == ["t1"]
    assert len(llm.calls) == 2


async def test_multiple_results_processed_independently() -> None:
    llm = FakeLLM(
        [
            json.dumps({"relevant": True, "line": "first result is in"}),
            json.dumps({"relevant": False, "line": ""}),
        ]
    )
    queue = FakeQueue([_task("t1"), _task("t2")])
    composer = DeliveryComposer(queue, llm)

    interjections = await composer.deliveries_for_pause("s1", "u_demo_001", "...")

    assert [i.task_id for i in interjections] == ["t1"]
    assert queue.delivered == ["t1"] and queue.suppressed == ["t2"]


# ── Item 8: pileup cap (never machine-gun) ────────────────────────────────


async def test_pileup_over_cap_becomes_one_summarize_and_offer() -> None:
    # Four finished, all relevant → must NOT dump 4; offer once instead.
    llm = FakeLLM([json.dumps({"relevant": True, "line": f"finding {i}"}) for i in range(4)])
    tasks = [_task(f"t{i}") for i in range(4)]
    queue = FakeQueue(tasks)
    composer = DeliveryComposer(queue, llm, max_interjections=2)

    interjections = await composer.deliveries_for_pause("s1", "u_demo_001", "...")

    assert len(interjections) == 1, "backlog should collapse to a single offer, not a machine-gun"
    assert "4 things" in interjections[0].line and "run through them" in interjections[0].line
    # All four are marked delivered so they don't re-fire next pause.
    assert set(queue.delivered) == {"t0", "t1", "t2", "t3"}


async def test_within_cap_delivers_each_directly() -> None:
    llm = FakeLLM([json.dumps({"relevant": True, "line": f"finding {i}"}) for i in range(2)])
    queue = FakeQueue([_task("t0"), _task("t1")])
    composer = DeliveryComposer(queue, llm, max_interjections=2)

    interjections = await composer.deliveries_for_pause("s1", "u_demo_001", "...")

    assert len(interjections) == 2
    assert set(queue.delivered) == {"t0", "t1"}


# ── U9: carry undelivered results to the next conversation open ───────────


def _prior_task(task_id: str, *, query: str, created_at: str, session: str = "s_old") -> QueuedTask:
    return QueuedTask(
        task_id=task_id,
        session_id=session,
        user_id="u_demo_001",
        type="web_search",
        params={"query": query},
        status="completed",
        result={"summary": f"result for {query}"},
        created_at=created_at,
    )


async def test_carry_result_from_a_prior_session_at_open() -> None:
    """A result that finished in a now-closed session is offered at the next open."""
    line = "oh hey — that book you asked me to look up, I found it"
    llm = FakeLLM([json.dumps({"relevant": True, "line": line})])
    # A durable ("look up a book") task from a prior session, not stale.
    queue = FakeQueue(
        [_prior_task("t_old", query="look up that book", created_at="2026-07-08T09:00:00+00:00")]
    )
    composer = DeliveryComposer(queue, llm)

    out = await composer.deliveries_at_open("u_demo_001", "s_new")

    assert len(out) == 1 and "found it" in out[0].line
    assert queue.delivered == ["t_old"]


async def test_stale_time_sensitive_result_dropped_at_open() -> None:
    """A "news today" result asked long ago has expired → dropped, never delivered late."""
    llm = FakeLLM([json.dumps({"relevant": True, "line": "should not be used"})])
    old = _prior_task("t_news", query="top news today", created_at="2026-07-01T09:00:00+00:00")
    queue = FakeQueue([old])
    composer = DeliveryComposer(queue, llm)

    out = await composer.deliveries_at_open("u_demo_001", "s_new")

    assert out == []
    assert queue.suppressed == ["t_news"]  # dropped as stale, no LLM composition


async def test_current_session_excluded_from_at_open() -> None:
    """The current session's results are handled by the in-session path, not carried."""
    llm = FakeLLM([json.dumps({"relevant": True, "line": "x"})])
    queue = FakeQueue(
        [
            _prior_task(
                "t_cur",
                query="look up a thing",
                created_at="2026-07-08T09:00:00+00:00",
                session="s_new",
            )
        ]
    )
    composer = DeliveryComposer(queue, llm)

    out = await composer.deliveries_at_open("u_demo_001", "s_new")

    assert out == []


async def test_stale_ones_purged_before_cap_counts() -> None:
    # 3 finished but 2 are stale → only 1 relevant remains → delivered directly,
    # NOT collapsed to an offer (the cap counts relevant results only).
    llm = FakeLLM(
        [
            json.dumps({"relevant": False, "line": ""}),
            json.dumps({"relevant": True, "line": "the real one"}),
            json.dumps({"relevant": False, "line": ""}),
        ]
    )
    queue = FakeQueue([_task("t0"), _task("t1"), _task("t2")])
    composer = DeliveryComposer(queue, llm, max_interjections=2)

    interjections = await composer.deliveries_for_pause("s1", "u_demo_001", "...")

    assert len(interjections) == 1 and interjections[0].line == "the real one"
    assert set(queue.suppressed) == {"t0", "t2"}
    assert queue.delivered == ["t1"]
