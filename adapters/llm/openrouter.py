"""Adapter: OpenRouter chat-completions router (implements ports.llm.LLM, spec §11).

Complexity tiers map to model chains from provider_config; each request walks
its chain on provider error/timeout (rule 2). OpenRouter's usage accounting
(``usage: {include: true}``) returns the exact cost per call, which is logged
to the Cost Ledger fire-and-forget after the call resolves (rule 3).
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from functools import cached_property
from typing import Any

from fastembed import TextEmbedding
from openai import AsyncOpenAI

from config.settings import Settings
from core.cost import CostEntry, CostLedger, CostMetadata
from core.observability.logger import StructuredLogger
from ports.llm import CompletionResult, LLMUnavailable, Tier

logger = logging.getLogger(__name__)


def _cached_tokens(usage: Any) -> int:
    """Prompt-cache read tokens from an OpenAI/OpenRouter usage object.

    Cache-supporting models report ``prompt_tokens_details.cached_tokens``; some
    providers surface ``cache_read_input_tokens`` in the usage extras instead. A
    value >0 is a cache hit (billed $0 for that portion)."""
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", None)
        if cached is None and isinstance(details, dict):
            cached = details.get("cached_tokens")
        if cached:
            return int(cached)
    extra = getattr(usage, "model_extra", None)
    if extra:
        return int(extra.get("cache_read_input_tokens") or 0)
    return 0


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
        logs: StructuredLogger | None = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._logs = logs
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

    def fast_model_choices(self) -> list[str]:
        """De-duped simple+moderate tier models — the user-selectable fast set (§4)."""
        seen: list[str] = []
        for tier in ("simple", "moderate"):
            for model in self._tiers.get(tier, []):
                if model not in seen:
                    seen.append(model)
        return seen

    def reasoning_model_choices(self) -> list[str]:
        """De-duped moderate+complex tier models — the user-selectable mature
        'thinking' set for the main reasoning turn (F8/A2)."""
        seen: list[str] = []
        for tier in ("complex", "moderate"):
            for model in self._tiers.get(tier, []):
                if model not in seen:
                    seen.append(model)
        return seen

    def _known_models(self) -> set[str]:
        """Every configured model across all tiers — the set an explicit user
        model override (fast OR reasoning) is validated against."""
        return {m for chain in self._tiers.values() for m in chain}

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
        missing = sorted({m for chain in self._tiers.values() for m in chain if m not in catalog})
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
        model: str | None = None,
    ) -> CompletionResult:
        errors: list[str] = []
        chain = list(self._tiers[tier])
        # §4/F8: a valid user-selected model (fast on sub-steps, or the mature
        # 'thinking' model on the main turn) is tried first; the tier chain remains
        # the fallback. Ignore an unknown id rather than trusting it blindly.
        if model and model in self._known_models():
            chain = [model, *[m for m in chain if m != model]]
        for model_id in chain:
            started = time.perf_counter()
            try:
                result = await self._call(model_id, messages, response_format, max_tokens)
            except Exception as exc:
                errors.append(f"{model_id}: {type(exc).__name__}: {exc}")
                logger.warning("LLM call failed on %s, trying fallback: %s", model_id, exc)
                continue
            self._log_cost(user_id, result, session_id)
            # Per-LLM-call span (CLAUDE.md §5): model / tokens / cost / latency,
            # correlation-bound to the current turn so it lands in the trace.
            self._log_call(result, tier, (time.perf_counter() - started) * 1000)
            return result
        raise LLMUnavailable(f"all models failed for tier '{tier}': {'; '.join(errors)}")

    async def stream(
        self,
        user_id: str,
        messages: Sequence[Mapping[str, Any]],
        tier: Tier = "moderate",
        *,
        response_format: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream text deltas from the first model in the chain (§8.12).

        No mid-stream fallback: if the model errors before any delta the caller
        falls back to non-streamed ``complete`` (which walks the whole chain).
        """
        chain = list(self._tiers[tier])
        if model and model in self._known_models():
            chain = [model, *[m for m in chain if m != model]]
        model_id = chain[0]
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": list(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "extra_body": {"usage": {"include": True}},
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise LLMUnavailable(f"stream failed to start on {model_id}: {exc}") from exc

        parts: list[str] = []
        usage: Any = None
        async for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta is not None and delta.content:
                    parts.append(delta.content)
                    yield delta.content
            if getattr(chunk, "usage", None) is not None:
                usage = chunk.usage

        result = self._result_from_usage("".join(parts), model_id, usage)
        self._log_cost(user_id, result, session_id)
        self._log_call(result, tier, (time.perf_counter() - started) * 1000)

    def _result_from_usage(self, text: str, model_id: str, usage: Any) -> CompletionResult:
        cost = 0.0
        in_tok = out_tok = 0
        if usage is not None:
            in_tok = getattr(usage, "prompt_tokens", 0) or 0
            out_tok = getattr(usage, "completion_tokens", 0) or 0
            if getattr(usage, "model_extra", None):
                cost = float(usage.model_extra.get("cost") or 0.0)
        return CompletionResult(
            text=text,
            model=model_id,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            cached_tokens=_cached_tokens(usage),
        )

    def preload(self) -> None:
        """Touch the local embedder so the first turn doesn't pay its cold load."""
        _ = self._embedder

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
            cached_tokens=_cached_tokens(usage),
        )

    def _log_call(self, result: CompletionResult, tier: Tier, latency_ms: float) -> None:
        if self._logs is None:
            return
        self._logs.log(
            "info",
            "llm.call",
            stage="llm",
            model=result.model,
            tier=tier,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            latency_ms=round(latency_ms, 1),
            # Item 7 prompt caching: how many input tokens were served from cache
            # (billed $0), and whether this call was a cache hit at all.
            cached_tokens=result.cached_tokens,
            cache_hit=result.cached_tokens > 0,
        )

    def _log_cost(self, user_id: str, result: CompletionResult, session_id: str | None) -> None:
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
