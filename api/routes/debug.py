"""Trace inspection endpoints (brief §1: inspect a turn's full trace after the fact).

Read-only, auth'd, and strictly user-scoped: a user can only ever see their own
persisted traces (§0.5 multi-tenant isolation). Backed by the ``turn_traces``
Mongo collection written during each voice conversation (``core/observability``).
"""

import hashlib
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from api.deps import CurrentUser
from core.observability.attribution import (
    attribute_by_prompt_version,
    prompt_version_by_turn,
)

router = APIRouter(prefix="/debug")


def _pipeline(request: Request) -> Any:
    pipeline = request.app.state.pipeline
    if pipeline is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="pipeline not wired"
        )
    return pipeline


def _trace_store(request: Request) -> Any:
    return _pipeline(request).traces


@router.get("/traces")
async def list_trace_sessions(user: CurrentUser, request: Request) -> dict[str, Any]:
    """This user's recent traced sessions (most recent first)."""
    sessions = await _trace_store(request).recent_sessions(user.user_id)
    return {"user_id": user.user_id, "sessions": sessions}


@router.get("/traces/{session_id}")
async def get_session_trace(session_id: str, user: CurrentUser, request: Request) -> dict[str, Any]:
    """The full A→Z per-turn trace for one of this user's sessions, plus a per-turn
    totals roll-up (§3.12: end-to-end latency + total tokens/cost) so a turn can be
    reconstructed AND its cost/latency read at a glance from the trace alone."""
    events = await _trace_store(request).traces_for(user.user_id, session_id)
    turns = _turn_totals(events)
    # A9: deep-link each turn to its full Langfuse trace (the hierarchical detail
    # view). Langfuse's create_trace_id(seed) is sha256(seed)[:32].
    settings = _pipeline(request).settings
    if settings.langfuse_enabled:
        for t in turns:
            seed = f"{session_id}:{t['turn']}"
            tid = hashlib.sha256(seed.encode()).hexdigest()[:32]
            t["langfuse_url"] = (
                f"{settings.langfuse_host}/project/{settings.langfuse_project}/traces/{tid}"
            )
    return {
        "user_id": user.user_id,
        "session_id": session_id,
        "events": events,
        "turns": turns,
    }


@router.get("/attribution")
async def prompt_version_attribution(user: CurrentUser, request: Request) -> dict[str, Any]:
    """Response-quality attribution grouped by prompt_version (Item 7 / §7): joins
    this user's thumbs feedback with the prompt_version on each turn's trace and
    rolls up a thumbs-up rate per version — so two prompt versions can be compared."""
    pipeline = _pipeline(request)
    feedback, _ = await pipeline.feedback.list_for_user(user.user_id, limit=100000)
    # Build (session, turn) → prompt_version across the sessions that have feedback.
    sessions = {fb.get("session_id", "") for fb in feedback}
    version_by_turn: dict[tuple[str, int], str] = {}
    for sid in sessions:
        if not sid:
            continue
        events = await pipeline.traces.traces_for(user.user_id, sid)
        version_by_turn.update(prompt_version_by_turn(events))
    rows = attribute_by_prompt_version(feedback, version_by_turn)
    return {"user_id": user.user_id, "by_prompt_version": rows}


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _turn_totals(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roll up per-turn cost/latency/step-counts from the raw spans."""
    by_turn: dict[int, dict[str, Any]] = {}
    for e in events:
        turn = int(e.get("turn", 0))
        data = e.get("data", {}) or {}
        t = by_turn.setdefault(
            turn,
            {
                "turn": turn,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_usd": 0.0,
                "llm_calls": 0,
                "tool_calls": 0,
                "failures": 0,
                "total_ms": 0.0,
                "reflected": False,
            },
        )
        # LLM-call spans carry input_tokens/output_tokens/cost_usd (either the
        # unified StepResult names or the OpenRouter span names).
        t["tokens_in"] += int(_num(data.get("tokens_in") or data.get("input_tokens")))
        t["tokens_out"] += int(_num(data.get("tokens_out") or data.get("output_tokens")))
        t["cost_usd"] += _num(data.get("usd") or data.get("cost_usd"))
        if e.get("stage") == "llm":
            t["llm_calls"] += 1
        if e.get("stage") == "tool":
            t["tool_calls"] += 1
            if data.get("status") in ("failure", "timeout"):
                t["failures"] += 1
        if e.get("stage") == "reflection":
            t["reflected"] = True
        if "total_ms" in data:  # the per-turn summary span
            t["total_ms"] = _num(data.get("total_ms"))
    for t in by_turn.values():
        t["cost_usd"] = round(t["cost_usd"], 6)
    return [by_turn[k] for k in sorted(by_turn)]
