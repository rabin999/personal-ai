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
        self._url_base: str | None | bool = False  # False = not resolved yet

    def trace_id_for(self, session_id: str, turn: int) -> str:
        """The deterministic Langfuse trace id for a (session, turn) — the SAME seed
        the sink writes under, so a deep-link resolves to the exact trace."""
        return self._lf.create_trace_id(seed=f"{session_id}:{turn}")

    def _resolve_url_base(self) -> str | None:
        """The ``.../project/{projectId}/traces/`` prefix, learned ONCE from the SDK
        (which knows the real project id + public host) and cached — so deep-links
        don't guess a project *name* (the old bug: links resolved to the wrong place)
        and don't pay a network call per turn."""
        if self._url_base is not False:
            return self._url_base  # type: ignore[return-value]
        base: str | None = None
        try:
            probe = self._lf.get_trace_url(trace_id="0" * 32)
            if probe and probe.endswith("0" * 32):
                base = probe[:-32]  # strip the probe trace id → reusable prefix
        except Exception:
            base = None
        self._url_base = base
        return base

    def trace_url(self, session_id: str, turn: int) -> str | None:
        """Browser URL for a turn's trace, with the real project id + host. Best-effort
        — a lookup failure returns None so the caller falls back / omits the link."""
        base = self._resolve_url_base()
        if not base:
            return None
        return f"{base}{self.trace_id_for(session_id, turn)}"

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
        from langfuse._client.propagation import propagate_attributes

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
        # propagate_attributes stamps user_id/session_id onto the trace, but ONLY on
        # spans created inside its context — so the observation MUST be started within
        # the `with`, not before it. Without this the trace has no user (the app is
        # multi-tenant; per-user filtering/cost in Langfuse depends on it — CLAUDE.md
        # §3 invariant 1).
        with propagate_attributes(user_id=user_id, session_id=session):
            observation = (
                self._lf.start_as_current_observation(
                    name=name, as_type="generation", trace_context=ctx
                )
                if is_generation
                else self._lf.start_as_current_observation(
                    name=name, as_type="span", trace_context=ctx
                )
            )
            with observation as span:
                update: dict[str, Any] = {"metadata": {"stage": stage, "session_id": session}}
                if is_generation:
                    # The real prompt (incl. the assembled system prompt) + the reply,
                    # so the generation shows its input/output, not an empty span.
                    if data.get("messages") is not None:
                        update["input"] = data["messages"]
                    if data.get("completion") is not None:
                        update["output"] = data["completion"]
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
                # Non-usage fields stay as metadata (prompt/reply already promoted to
                # input/output above are dropped from the blob to avoid dupes).
                update["metadata"].update(
                    {k: v for k, v in data.items() if k not in ("messages", "completion")}
                )
                span.update(**update)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._lf.flush()


class LangfuseScoreSink:
    """Attach human/eval scores to the matching Langfuse trace (F13).

    Uses the SAME ``create_trace_id(seed="{session}:{turn}")`` the trace sink uses,
    so a thumbs-up/down lands on the exact trace that produced the reply. Best-effort
    — a scoring failure is swallowed so feedback submission never fails the request.
    """

    def __init__(self, public_key: str, secret_key: str, host: str) -> None:
        from langfuse import Langfuse  # imported only in the adapter

        self._lf = Langfuse(public_key=public_key, secret_key=secret_key, host=host)

    def score(
        self, *, session_id: str, turn: int, name: str, value: float, comment: str = ""
    ) -> None:
        try:
            trace_id = self._lf.create_trace_id(seed=f"{session_id}:{turn}")
            self._lf.create_score(
                name=name,
                value=value,
                trace_id=trace_id,
                session_id=session_id,
                comment=comment or None,
                data_type="NUMERIC",
            )
            self._lf.flush()
        except Exception:
            logger.debug("langfuse score submit failed", exc_info=True)
