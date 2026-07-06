"""Integration tests for the LLM Router (spec §11) — real OpenRouter calls.

Cheap: tiny prompts with small max_tokens on flash-lite class models.
Skipped loudly without OPEN_ROUTER_API_KEY.
"""

import uuid
from collections.abc import AsyncIterator

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from adapters.llm.openrouter import OpenRouterLLM
from config.settings import get_settings
from core.cost import CostLedger

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not get_settings().open_router_api_key,
        reason="OPEN_ROUTER_API_KEY not set — §11 needs real OpenRouter",
    ),
]

MESSAGES = [{"role": "user", "content": "Reply with exactly: pong"}]


@pytest.fixture
def user_id() -> str:
    return f"it_{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def real_db() -> AsyncIterator[Database]:
    database = Database(get_settings())
    yield database
    await database.mongo("cost_ledger").delete_many({"user_id": {"$regex": "^it_"}})
    await database.aclose()


@pytest.fixture
def ledger(real_db: Database) -> CostLedger:
    return CostLedger(MongoDocStore(real_db))


@pytest.fixture
def router(ledger: CostLedger) -> OpenRouterLLM:
    return OpenRouterLLM(get_settings(), ledger=ledger)


async def test_simple_tier_resolves_and_answers(router: OpenRouterLLM, user_id: str) -> None:
    result = await router.complete(user_id, MESSAGES, "simple", max_tokens=2000)
    assert "pong" in result.text.lower()
    assert result.model.startswith(router.route("simple").split(":")[0])
    assert result.input_tokens > 0 and result.output_tokens > 0


async def test_fallback_recovers_from_a_dead_primary(ledger: CostLedger, user_id: str) -> None:
    router = OpenRouterLLM(
        get_settings(),
        ledger=ledger,
        tiers={"simple": ["fake/model-does-not-exist", "google/gemini-2.5-flash-lite"]},
    )
    result = await router.complete(user_id, MESSAGES, "simple", max_tokens=2000)
    assert "pong" in result.text.lower()
    assert "gemini" in result.model


async def test_real_cost_lands_in_ledger(
    router: OpenRouterLLM, ledger: CostLedger, user_id: str
) -> None:
    result = await router.complete(user_id, MESSAGES, "simple", max_tokens=2000)
    await ledger.flush()

    summary = await ledger.get(user_id, component="llm")
    assert summary.count == 1
    assert summary.total_usd == pytest.approx(result.cost_usd)
    assert result.cost_usd > 0  # OpenRouter usage accounting returned real cost
