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
