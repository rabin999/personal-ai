"""Adapter: OpenRouter chat-completions router (implements ports.llm.LLM, spec §11).

Complexity tiers map to model chains from provider_config; each request walks
its chain on provider error/timeout (rule 2). OpenRouter's usage accounting
(``usage: {include: true}``) returns the exact cost per call, which is logged
to the Cost Ledger fire-and-forget after the call resolves (rule 3).
"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from functools import cached_property
from typing import Any

from fastembed import TextEmbedding
from openai import AsyncOpenAI

from config.settings import Settings
from core.cost import CostEntry, CostLedger, CostMetadata
from ports.llm import CompletionResult, LLMUnavailable, Tier

logger = logging.getLogger(__name__)

# Fallback chains if provider_config carries no llm_router document.
DEFAULT_TIERS: dict[str, list[str]] = {
    "simple": ["google/gemini-2.5-flash-lite", "openai/gpt-4.1-nano"],
    "moderate": ["google/gemini-2.5-flash", "openai/gpt-4.1-mini"],
    "complex": ["anthropic/claude-sonnet-4.5", "google/gemini-2.5-pro"],
}


class OpenRouterLLM:
    def __init__(
        self,
        settings: Settings,
        ledger: CostLedger | None = None,
        tiers: Mapping[str, list[str]] | None = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._tiers = {**DEFAULT_TIERS, **dict(tiers or {})}
        self._client = AsyncOpenAI(
            api_key=settings.open_router_api_key,
            base_url=settings.open_router_base_url,
            timeout=settings.llm_timeout_s,
            max_retries=0,  # fallback policy lives here, not in the SDK
        )

    @cached_property
    def _embedder(self) -> TextEmbedding:
        return TextEmbedding(self._settings.embedding_model)

    def route(self, complexity: Tier) -> str:
        return self._tiers[complexity][0]

    async def verify_models(self) -> dict[str, list[str]]:
        """Check configured tier models against the live OpenRouter catalog.

        Returns ``{"missing": [...], "no_fallback": [...]}`` — missing model
        ids (typo/deprecation) and tiers with fewer than two models (no
        fallback). Called at startup so a bad model id fails loud early rather
        than mid-conversation.
        """
        try:
            page = await self._client.models.list()
            catalog = {model.id for model in page.data}
        except Exception as exc:  # catalog unreachable → skip (don't block startup)
            logger.warning("could not fetch OpenRouter catalog to verify models: %s", exc)
            return {"missing": [], "no_fallback": []}
        missing = sorted(
            {m for chain in self._tiers.values() for m in chain if m not in catalog}
        )
        no_fallback = sorted(t for t, chain in self._tiers.items() if len(chain) < 2)
        if missing:
            logger.error("configured LLM models not in OpenRouter catalog: %s", missing)
        if no_fallback:
            logger.warning("LLM tiers without a fallback model: %s", no_fallback)
        return {"missing": missing, "no_fallback": no_fallback}

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
        errors: list[str] = []
        for model in self._tiers[tier]:
            try:
                result = await self._call(model, messages, response_format, max_tokens)
            except Exception as exc:
                errors.append(f"{model}: {type(exc).__name__}: {exc}")
                logger.warning("LLM call failed on %s, trying fallback: %s", model, exc)
                continue
            self._log_cost(user_id, result, session_id)
            return result
        raise LLMUnavailable(f"all models failed for tier '{tier}': {'; '.join(errors)}")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(
            lambda: [vector.tolist() for vector in self._embedder.embed(texts)]
        )

    async def _call(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        response_format: Mapping[str, Any] | None,
        max_tokens: int | None,
    ) -> CompletionResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            # OpenRouter usage accounting: exact cost in the response.
            "extra_body": {"usage": {"include": True}},
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        usage = response.usage
        cost = 0.0
        if usage is not None and getattr(usage, "model_extra", None):
            cost = float(usage.model_extra.get("cost") or 0.0)
        return CompletionResult(
            text=choice.message.content or "",
            model=response.model or model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            cost_usd=cost,
        )

    def _log_cost(
        self, user_id: str, result: CompletionResult, session_id: str | None
    ) -> None:
        if self._ledger is None:
            return
        self._ledger.log(
            CostEntry(
                user_id=user_id,
                component="llm",
                provider="openrouter",
                units={
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                },
                cost_usd=result.cost_usd,
                metadata=CostMetadata(session_id=session_id),
            )
        )
