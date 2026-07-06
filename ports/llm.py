"""Port: LLM chat completions via OpenRouter — complexity-tier routing, fallback (spec §11).

``complete`` takes the resolved user_id so every call can be cost-attributed
(§0.5); the judgment block inside the response text is parsed and validated
by Response Generation (§12), not here.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel

Tier = Literal["simple", "moderate", "complex"]


class CompletionResult(BaseModel):
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class LLMUnavailable(Exception):
    """Every model in the tier's fallback chain failed (spec §11 rule 2)."""


class LLM(Protocol):
    async def complete(
        self,
        user_id: str,
        messages: Sequence[Mapping[str, Any]],
        tier: Tier = "moderate",
        *,
        response_format: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        """Route to the tier's model chain; raise LLMUnavailable if all fail."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embedding vectors (local fastembed — OpenRouter has no embeddings API)."""
        ...

    def route(self, complexity: Tier) -> str:
        """The model id a given complexity tier resolves to first."""
        ...
