"""Port: LLM chat completions via OpenRouter — complexity-tier routing, fallback (spec §11).

``complete`` takes the resolved user_id so every call can be cost-attributed
(§0.5); the judgment block inside the response text is parsed and validated
by Response Generation (§12), not here.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel

Tier = Literal["simple", "moderate", "complex"]


class CompletionResult(BaseModel):
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    # Prompt-cache read tokens (Item 7): the portion of the input served from the
    # provider's prompt cache. >0 means a cache hit — those tokens are billed at $0
    # (already reflected in cost_usd); recorded so hit/miss is visible in the trace.
    cached_tokens: int = 0


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
        model: str | None = None,
    ) -> CompletionResult:
        """Route to the tier's model chain; raise LLMUnavailable if all fail.

        ``model`` (a user-selected fast model, §4) is tried first when given,
        with the tier chain kept as fallback.
        """
        ...

    def stream(
        self,
        user_id: str,
        messages: Sequence[Mapping[str, Any]],
        tier: Tier = "moderate",
        *,
        response_format: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream the completion as text deltas (spec §8.12 — start TTS on the
        first sentence). Cost + the per-call span are logged when the stream ends.
        Raises LLMUnavailable if the model can't start; the caller may fall back to
        the non-streamed ``complete``.
        """
        ...

    def fast_model_choices(self) -> list[str]:
        """The user-selectable fast/flash models (simple+moderate tiers, §4)."""
        ...

    def reasoning_model_choices(self) -> list[str]:
        """The user-selectable mature 'thinking' models for the main turn (F8/A2)."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embedding vectors (local fastembed — OpenRouter has no embeddings API)."""
        ...

    def route(self, complexity: Tier) -> str:
        """The model id a given complexity tier resolves to first."""
        ...
