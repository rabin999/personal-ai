"""Unit tests for the LLM Router (spec §11) — OpenAI client faked."""

from types import SimpleNamespace
from typing import Any

import pytest

from adapters.llm.openrouter import OpenRouterLLM
from config.settings import Settings
from core.cost import COST_COLLECTION, CostLedger
from ports.llm import LLMUnavailable
from tests.fakes import FakeDocStore

TIERS = {
    "simple": ["cheap/model", "cheap/backup"],
    "moderate": ["mid/model"],
    "complex": ["strong/model"],
}


def _response(model: str, text: str = "hello", cost: float = 0.00123) -> SimpleNamespace:
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20, model_extra={"cost": cost})
    message = SimpleNamespace(content=text)
    return SimpleNamespace(model=model, usage=usage, choices=[SimpleNamespace(message=message)])


class FakeCompletions:
    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = failing or set()
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        model = kwargs["model"]
        if model in self.failing:
            raise TimeoutError(f"{model} timed out")
        return _response(model)


def _router(
    failing: set[str] | None = None, ledger: CostLedger | None = None
) -> tuple[OpenRouterLLM, FakeCompletions]:
    router = OpenRouterLLM(
        Settings(_env_file=None, open_router_api_key="test-key"), ledger=ledger, tiers=TIERS
    )
    fake = FakeCompletions(failing)
    router._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))  # type: ignore[assignment]
    return router, fake


MESSAGES = [{"role": "user", "content": "hi"}]


# Acceptance: simple tier hits the cheap model, complex hits the strong one.
async def test_tier_selects_the_configured_model() -> None:
    router, fake = _router()
    simple = await router.complete("u_demo_001", MESSAGES, "simple")
    complex_ = await router.complete("u_demo_001", MESSAGES, "complex")

    assert simple.model == "cheap/model"
    assert complex_.model == "strong/model"
    assert [c["model"] for c in fake.calls] == ["cheap/model", "strong/model"]


def test_route_returns_first_model_of_tier() -> None:
    router, _ = _router()
    assert router.route("moderate") == "mid/model"


# Acceptance: primary failure triggers fallback and still returns a result.
async def test_primary_failure_falls_back_to_next_model() -> None:
    router, fake = _router(failing={"cheap/model"})
    result = await router.complete("u_demo_001", MESSAGES, "simple")
    assert result.model == "cheap/backup"
    assert [c["model"] for c in fake.calls] == ["cheap/model", "cheap/backup"]


async def test_all_models_failing_raises_structured_error() -> None:
    router, _ = _router(failing={"cheap/model", "cheap/backup"})
    with pytest.raises(LLMUnavailable, match="simple"):
        await router.complete("u_demo_001", MESSAGES, "simple")


# Acceptance: every completion produces a cost-ledger entry with usage.
async def test_completion_logs_cost_entry_with_usage() -> None:
    docs = FakeDocStore()
    ledger = CostLedger(docs)
    router, _ = _router(ledger=ledger)

    await router.complete("u_demo_001", MESSAGES, "simple", session_id="s1")
    await ledger.flush()

    (row,) = await docs.find(COST_COLLECTION)
    assert row["user_id"] == "u_demo_001"
    assert row["component"] == "llm"
    assert row["units"] == {"input_tokens": 100, "output_tokens": 20}
    assert row["cost_usd"] == pytest.approx(0.00123)
    assert row["metadata"]["session_id"] == "s1"


async def test_failed_call_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    docs = FakeDocStore()
    ledger = CostLedger(docs)
    router, _ = _router(failing={"cheap/model", "cheap/backup"}, ledger=ledger)
    with pytest.raises(LLMUnavailable):
        await router.complete("u_demo_001", MESSAGES, "simple")
    await ledger.flush()
    assert await docs.find(COST_COLLECTION) == []


async def test_usage_accounting_is_requested_from_openrouter() -> None:
    router, fake = _router()
    await router.complete("u_demo_001", MESSAGES, "simple")
    assert fake.calls[0]["extra_body"] == {"usage": {"include": True}}


async def test_response_format_and_max_tokens_pass_through() -> None:
    router, fake = _router()
    await router.complete(
        "u_demo_001",
        MESSAGES,
        "simple",
        response_format={"type": "json_object"},
        max_tokens=64,
    )
    assert fake.calls[0]["response_format"] == {"type": "json_object"}
    assert fake.calls[0]["max_tokens"] == 64
