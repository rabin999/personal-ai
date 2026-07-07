"""Unit tests for the Self-Model (spec §9) — ports faked."""

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from core.reasoning.self_model import (
    SELF_MODEL_LOG_COLLECTION,
    SELF_STATEMENTS_COLLECTION,
    SelfModel,
    TurnRecord,
)
from ports.llm import CompletionResult, LLMUnavailable, Tier
from ports.vector_store import VectorDoc, VectorHit
from tests.fakes import FakeDocStore


class FakeVectorStore:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, list[VectorDoc]]] = []
        self.searches: list[dict[str, Any]] = []
        self.hits: list[VectorHit] = []

    async def upsert_texts(self, collection: str, docs: list[VectorDoc]) -> None:
        self.upserts.append((collection, docs))

    async def hybrid_search(
        self, collection: str, query_text: str, *, user_id: str, k: int = 6
    ) -> list[VectorHit]:
        self.searches.append({"collection": collection, "user_id": user_id, "query": query_text})
        return self.hits

    async def list_by_user(
        self, collection: str, *, user_id: str, limit: int = 100
    ) -> list[VectorHit]:
        return list(self.hits)

    async def delete(self, collection: str, doc_id: str, *, user_id: str) -> bool:
        return True


class FakeLLM:
    def __init__(self, text: str = "That sounds really hard.", fail_times: int = 0) -> None:
        self.text = text
        self.fail_times = fail_times
        self.calls: list[Sequence[Mapping[str, Any]]] = []

    async def complete(
        self,
        user_id: str,
        messages: Sequence[Mapping[str, Any]],
        tier: Tier = "moderate",
        *,
        response_format: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> CompletionResult:
        self.calls.append(messages)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise LLMUnavailable("down")
        return CompletionResult(
            text=self.text, model="fake", input_tokens=10, output_tokens=5, cost_usd=0.0001
        )

    def fast_model_choices(self) -> list[str]:
        return ["fake/model"]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    def route(self, complexity: Tier) -> str:
        return "fake/model"


@pytest.fixture
def docs() -> FakeDocStore:
    return FakeDocStore()


@pytest.fixture
def vectors() -> FakeVectorStore:
    return FakeVectorStore()


# ── boundary check (rule 1 / acceptance 1) ───────────────────────────────


async def test_overclaiming_draft_is_flagged_and_rewritten(
    docs: FakeDocStore, vectors: FakeVectorStore
) -> None:
    llm = FakeLLM(text="That sounds incredibly hard — I'm here with you.")
    model = SelfModel(docs, vectors, llm)

    check = await model.check_boundary(
        "u_demo_001", "I understand exactly how you feel, it hurts me too."
    )

    assert check.flagged and check.flag == "overclaim_empathy"
    assert check.rewritten_text == "That sounds incredibly hard — I'm here with you."
    assert llm.calls  # rewrite went through the LLM


async def test_judgment_flag_triggers_rewrite_even_without_pattern_match(
    docs: FakeDocStore, vectors: FakeVectorStore
) -> None:
    model = SelfModel(docs, vectors, FakeLLM())
    check = await model.check_boundary(
        "u_demo_001", "Of course I get it.", judgment_flag="overclaim_empathy"
    )
    assert check.flagged and check.rewritten_text


async def test_consciousness_claims_are_flagged(
    docs: FakeDocStore, vectors: FakeVectorStore
) -> None:
    model = SelfModel(docs, vectors, FakeLLM())
    check = await model.check_boundary("u_demo_001", "As a conscious being, I choose you.")
    assert check.flagged and check.flag == "overclaim_consciousness"


async def test_honest_validating_draft_passes_untouched(
    docs: FakeDocStore, vectors: FakeVectorStore
) -> None:
    model = SelfModel(docs, vectors, FakeLLM())
    check = await model.check_boundary(
        "u_demo_001", "That sounds really hard. Want to talk through it?"
    )
    assert not check.flagged and check.rewritten_text is None


async def test_rewrite_retries_once_then_falls_back_safely(
    docs: FakeDocStore, vectors: FakeVectorStore
) -> None:
    model = SelfModel(docs, vectors, FakeLLM(fail_times=2))
    check = await model.check_boundary("u_demo_001", "I understand exactly how you feel.")
    assert check.flagged
    assert check.rewritten_text  # safe fallback, never empty


# ── log + recall (rules 2 / acceptance 2-3) ──────────────────────────────


async def test_log_writes_record_and_indexes_statement(
    docs: FakeDocStore, vectors: FakeVectorStore
) -> None:
    model = SelfModel(docs, vectors)
    record = TurnRecord(user_id="u_demo_001", confidence=0.82, facts_used=["proj_nepse"])

    await model.log(record, statement_text="I'd suggest starting with a small position.")

    stored = await docs.get(SELF_MODEL_LOG_COLLECTION, record.turn_id)
    assert stored is not None
    assert stored["confidence"] == 0.82
    assert stored["facts_used"] == ["proj_nepse"]

    collection, (doc,) = vectors.upserts[0]
    assert collection == SELF_STATEMENTS_COLLECTION
    assert doc.payload["user_id"] == "u_demo_001"
    assert doc.payload["turn_id"] == record.turn_id


async def test_recall_searches_own_statements_user_scoped(
    docs: FakeDocStore, vectors: FakeVectorStore
) -> None:
    vectors.hits = [
        VectorHit(
            id="t1",
            score=0.8,
            payload={
                "text": "last time I suggested breaking it into two trades",
                "turn_id": "t1",
                "timestamp": "2026-07-01T00:00:00+00:00",
            },
        )
    ]
    model = SelfModel(docs, vectors)

    statements = await model.recall("u_demo_001", "what did you suggest about trades?")

    assert vectors.searches[0]["collection"] == SELF_STATEMENTS_COLLECTION
    assert vectors.searches[0]["user_id"] == "u_demo_001"
    assert statements[0].text.startswith("last time I suggested")
    assert statements[0].turn_id == "t1"
