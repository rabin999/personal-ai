"""Per-turn LLM-as-judge evaluator (CLAUDE.md §6/§7).

Asserts the evaluator runs the judge and posts its verdict as scores on the SAME
(session, turn) so Langfuse shows quality next to the pipeline — and that it stays
OFF unless explicitly enabled (an extra judge call per turn costs money).
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from core.eval.evaluator import SCORE_CHATBOT, SCORE_QUALITY, TurnEvaluator
from ports.llm import LLM


class _FakeLLM:
    """Minimal judge LLM — only ``.complete`` is exercised by the evaluator."""

    def __init__(self, verdict_json: str) -> None:
        self._json = verdict_json
        self.calls = 0

    async def complete(self, user_id: str, messages: Any, tier: str = "moderate", **_: Any) -> Any:
        self.calls += 1
        from ports.llm import CompletionResult

        return CompletionResult(
            text=self._json, model="judge/x", input_tokens=1, output_tokens=1, cost_usd=0.0
        )


def _ev(llm: _FakeLLM, scores: Any, *, enabled: bool) -> TurnEvaluator:
    # The evaluator only calls .complete; cast past the full LLM protocol for the fake.
    return TurnEvaluator(cast(LLM, llm), scores, enabled=enabled)


class _RecordingScores:
    def __init__(self) -> None:
        self.scores: list[dict[str, Any]] = []

    def score(
        self, *, session_id: str, turn: int, name: str, value: float, comment: str = ""
    ) -> None:
        self.scores.append({"session_id": session_id, "turn": turn, "name": name, "value": value})


async def test_evaluator_posts_quality_and_chatbot_scores() -> None:
    llm = _FakeLLM('{"chatbot_like": false, "companion_score": 4, "reason": "warm and present"}')
    scores = _RecordingScores()
    ev = _ev(llm, scores, enabled=True)
    await ev._evaluate("sessX", 3, "hey", "good to hear from you!")

    by_name = {s["name"]: s for s in scores.scores}
    assert by_name[SCORE_QUALITY]["value"] == 4.0
    assert by_name[SCORE_CHATBOT]["value"] == 0.0
    # Both land on the exact (session, turn) the reply produced.
    assert all(s["session_id"] == "sessX" and s["turn"] == 3 for s in scores.scores)


async def test_chatbot_like_reply_flagged() -> None:
    llm = _FakeLLM('{"chatbot_like": true, "companion_score": 1, "reason": "How can I help you?"}')
    scores = _RecordingScores()
    ev = _ev(llm, scores, enabled=True)
    await ev._evaluate("s", 1, "hi", "How can I help you today?")
    assert {s["name"]: s["value"] for s in scores.scores}[SCORE_CHATBOT] == 1.0


def test_disabled_by_default_does_not_call_judge() -> None:
    llm = _FakeLLM("{}")
    scores = _RecordingScores()
    ev = _ev(llm, scores, enabled=False)
    assert ev.enabled is False
    ev.schedule(session_id="s", turn=1, user_msg="hi", reply="hey")
    assert llm.calls == 0 and scores.scores == []


def test_no_score_sink_disables_even_when_enabled() -> None:
    # Enabling eval without a backend to post to is a no-op, not a crash.
    ev = _ev(_FakeLLM("{}"), None, enabled=True)
    assert ev.enabled is False
    ev.schedule(session_id="s", turn=1, user_msg="hi", reply="hey")


@pytest.mark.asyncio
async def test_schedule_runs_evaluation_in_background() -> None:
    import asyncio

    llm = _FakeLLM('{"chatbot_like": false, "companion_score": 5, "reason": "great"}')
    scores = _RecordingScores()
    ev = _ev(llm, scores, enabled=True)
    ev.schedule(session_id="s", turn=2, user_msg="hi", reply="hey friend")
    # Let the fire-and-forget task run.
    await asyncio.sleep(0.05)
    assert {s["name"] for s in scores.scores} == {SCORE_QUALITY, SCORE_CHATBOT}
