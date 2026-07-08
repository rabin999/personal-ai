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


def _with_cache_control(
    messages: Sequence[Mapping[str, Any]], cache_prefix: str, model: str
) -> list[Mapping[str, Any]]:
    """Place an Anthropic prompt-cache breakpoint on the stable system prefix (L6).

    Anthropic needs an explicit ``cache_control`` marker to cache a prefix; Gemini and
    OpenAI cache identical prefixes implicitly, so we only restructure for Anthropic
    models. The system message content is split into a cached prefix block + the rest.
    Any mismatch (prefix isn't actually the leading text) falls back to the messages
    unchanged — caching is a pure optimization, never a correctness risk."""
    msgs = list(messages)
    if not cache_prefix or "anthropic" not in model.lower() or not msgs:
        return msgs
    head = msgs[0]
    content = head.get("content")
    if head.get("role") != "system" or not isinstance(content, str):
        return msgs
    if not content.startswith(cache_prefix):
        return msgs
    rest = content[len(cache_prefix) :]
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}}
    ]
    if rest.strip():
        blocks.append({"type": "text", "text": rest})
    return [{"role": "system", "content": blocks}, *msgs[1:]]


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


# Cap per-field prompt/reply text in the durable trace so a long context can't bloat
# the trace store; the head is what a human needs to see the prompt shape anyway.
_MAX_TRACE_CHARS = 20_000


def _trim_messages(
    messages: "Sequence[Mapping[str, Any]] | None",
) -> list[dict[str, Any]] | None:
    """Role + bounded content for each message, so the trace shows the real prompt
    (system + context + user) without persisting an unbounded blob."""
    if not messages:
        return None
    trimmed: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str) and len(content) > _MAX_TRACE_CHARS:
            content = content[:_MAX_TRACE_CHARS] + "…"
        trimmed.append({"role": m.get("role", ""), "content": content})
    return trimmed


# Fallback chains if provider_config carries no llm_router document.
DEFAULT_TIERS: dict[str, list[str]] = {
    "simple": ["google/gemini-2.5-flash", "google/gemini-2.5-flash-lite", "openai/gpt-4.1-nano"],
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
        self._catalog: list[str] = []  # live OpenRouter model ids (cached at startup)
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

    def catalog_models(self) -> list[str]:
        """The full live OpenRouter model catalog (cached at startup) — so the UI can
        offer EVERY model, not just the few configured tiers, with a search filter.
        Empty if the catalog was unreachable (UI then falls back to the tier choices)."""
        return list(self._catalog)

    def is_selectable_model(self, model: str) -> bool:
        """A user may pick any real catalog model (or a configured one if the catalog
        is unavailable). Guards against a garbage id being persisted."""
        if self._catalog:
            return model in self._catalog
        return model in self._known_models()

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
            self._catalog = sorted(catalog)  # cache for the model-picker (full list)
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
        temperature: float | None = None,
        cache_prefix: str = "",
        purpose: str = "",
    ) -> CompletionResult:
        errors: list[str] = []
        chain = list(self._tiers[tier])
        # §4/F8: a valid user-selected model (fast on sub-steps, or the mature
        # 'thinking' model on the main turn) is tried first; the tier chain remains
        # the fallback. Any REAL catalog model is honored (not just configured tiers).
        if model and self.is_selectable_model(model):
            chain = [model, *[m for m in chain if m != model]]
        for model_id in chain:
            wall_start = time.time()
            started = time.perf_counter()
            try:
                result = await self._call(
                    model_id, messages, response_format, max_tokens, temperature, cache_prefix
                )
            except Exception as exc:
                errors.append(f"{model_id}: {type(exc).__name__}: {exc}")
                logger.warning("LLM call failed on %s, trying fallback: %s", model_id, exc)
                continue
            self._log_cost(user_id, result, session_id)
            # Per-LLM-call span (CLAUDE.md §5 / C1): model / tokens / cost / latency +
            # the actual prompt/reply + the call's PURPOSE + full params + precise
            # start/end wall-clock (so the trace can show ordering AND parallel-vs-
            # sequential concurrency), correlation-bound to the current turn.
            self._log_call(
                result,
                tier,
                (time.perf_counter() - started) * 1000,
                messages,
                purpose=purpose,
                params=self._params(model_id, response_format, max_tokens, temperature, False),
                wall_start=wall_start,
            )
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
        temperature: float | None = None,
        cache_prefix: str = "",
        purpose: str = "",
    ) -> AsyncIterator[str]:
        """Stream text deltas from the first model in the chain (§8.12).

        No mid-stream fallback: if the model errors before any delta the caller
        falls back to non-streamed ``complete`` (which walks the whole chain).
        """
        chain = list(self._tiers[tier])
        if model and self.is_selectable_model(model):
            chain = [model, *[m for m in chain if m != model]]
        model_id = chain[0]
        wall_start = time.time()
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": _with_cache_control(messages, cache_prefix, model_id),
            "stream": True,
            "stream_options": {"include_usage": True},
            "extra_body": {"usage": {"include": True}},
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if temperature is not None:
            kwargs["temperature"] = temperature
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
        self._log_call(
            result,
            tier,
            (time.perf_counter() - started) * 1000,
            messages,
            purpose=purpose or "response",
            params=self._params(model_id, response_format, None, temperature, True),
            wall_start=wall_start,
        )

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
        temperature: float | None = None,
        cache_prefix: str = "",
    ) -> CompletionResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": _with_cache_control(messages, cache_prefix, model),
            # OpenRouter usage accounting: exact cost in the response.
            "extra_body": {"usage": {"include": True}},
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
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

    @staticmethod
    def _params(
        model_id: str,
        response_format: Mapping[str, Any] | None,
        max_tokens: int | None,
        temperature: float | None,
        streamed: bool,
    ) -> dict[str, Any]:
        """The FULL request params for the call (C1) — so the trace shows exactly how
        each model was invoked. ``temperature`` shows the provider default when the
        app didn't pin one (we don't send a value, so the model's own default holds)."""
        return {
            "model": model_id,
            "temperature": "provider default" if temperature is None else temperature,
            "max_tokens": "unbounded" if max_tokens is None else max_tokens,
            "response_format": (response_format or {}).get("type", "text"),
            "streamed": streamed,
        }

    def _log_call(
        self,
        result: CompletionResult,
        tier: Tier,
        latency_ms: float,
        messages: Sequence[Mapping[str, Any]] | None = None,
        *,
        purpose: str = "",
        params: dict[str, Any] | None = None,
        wall_start: float | None = None,
    ) -> None:
        if self._logs is None:
            return
        end = time.time()
        self._logs.log(
            "info",
            "llm.call",
            stage="llm",
            # C1: the CALL'S ROLE this turn (context_intent / response / reflection /
            # judge / memory_extraction / …) so the trace shows WHY each call ran.
            purpose=purpose or "unlabeled",
            model=result.model,
            tier=tier,
            # C1: the full request params (temperature, max_tokens, response_format,
            # streamed) that produced this call.
            params=params or {},
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            latency_ms=round(latency_ms, 1),
            # C1: precise wall-clock window so the UI can order the calls AND detect
            # which ran concurrently (parallel) vs. one-after-another (sequential).
            start_ts=round(wall_start, 4) if wall_start is not None else None,
            end_ts=round(end, 4),
            # Item 7 prompt caching: how many input tokens were served from cache
            # (billed $0), and whether this call was a cache hit at all.
            cached_tokens=result.cached_tokens,
            cache_hit=result.cached_tokens > 0,
            # The ACTUAL prompt (incl. the assembled system prompt) and the reply, so
            # the Langfuse generation shows its input/output instead of an empty span
            # (CLAUDE.md §5: per-LLM-call trace must be complete). Bounded to keep the
            # durable trace store lean.
            messages=_trim_messages(messages),
            completion=result.text[:_MAX_TRACE_CHARS],
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
