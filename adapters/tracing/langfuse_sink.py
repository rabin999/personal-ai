"""Langfuse trace sink (A8): route the per-turn trace into self-hosted Langfuse.

Implements the `ports.log_sink.LogSink` port, so Langfuse is a SWAPPABLE tracing
backend behind the same boundary as the file/stdout/Mongo sinks (A1.5) — `core/`
depends only on the port. Every structured log record bound to a turn
(`trace_id`=session, `turn_id`, `user_id`, `stage`) becomes a Langfuse observation
grouped under one trace per (session, turn), so the full pipeline — LLM calls with
model/tokens/cost, tool calls, reasoning nodes (incl. the why-not, A5), memory,
reflection — is queryable in the Langfuse UI with hierarchical spans, cost and
latency. Never raises: tracing must not break a turn.
"""

import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

# stages that represent a model call → logged as a Langfuse "generation" (gets
# model/usage/cost columns); everything else is a plain span.
_GENERATION_STAGES = {"llm"}


class LangfuseTraceSink:
    def __init__(self, public_key: str, secret_key: str, host: str) -> None:
        from langfuse import Langfuse  # imported only in the adapter

        self._lf = Langfuse(public_key=public_key, secret_key=secret_key, host=host)

    def write(self, record: dict[str, Any]) -> None:
        user_id = record.get("user_id")
        session = record.get("trace_id")
        if not user_id or not session:
            return  # not a per-turn record; other sinks still have it
        try:
            self._emit(str(user_id), str(session), record)
        except Exception:
            logger.debug("langfuse trace emit failed", exc_info=True)

    def _emit(self, user_id: str, session: str, record: dict[str, Any]) -> None:
        turn = int(record.get("turn_id", 0) or 0)
        stage = str(record.get("stage", "log"))
        trace_id = self._lf.create_trace_id(seed=f"{session}:{turn}")
        data = {
            k: v
            for k, v in record.items()
            if k not in ("user_id", "trace_id", "turn_id", "ts", "level", "event", "stage")
        }
        is_generation = stage in _GENERATION_STAGES
        name = str(record.get("event", stage) or stage)
        ctx: Any = {"trace_id": trace_id}
        span = (
            self._lf.start_observation(name=name, as_type="generation", trace_context=ctx)
            if is_generation
            else self._lf.start_observation(name=name, as_type="span", trace_context=ctx)
        )
        update: dict[str, Any] = {"metadata": {"stage": stage, "session_id": session, **data}}
        if is_generation:
            if data.get("model"):
                update["model"] = data["model"]
            usage = {}
            if data.get("input_tokens") is not None:
                usage["input"] = int(data["input_tokens"])
            if data.get("output_tokens") is not None:
                usage["output"] = int(data["output_tokens"])
            if usage:
                update["usage_details"] = usage
            if data.get("cost_usd") is not None:
                update["cost_details"] = {"total": float(data["cost_usd"])}
        span.update(**update)
        span.end()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._lf.flush()
