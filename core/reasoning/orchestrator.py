"""Orchestrator port (A1.5): the reasoning/turn-orchestration engine, behind an
interface so the concrete engine is swappable.

The turn engine is NOT allowed to be deeply coupled into the app. `core/` and the
serving edges depend only on this `Orchestrator` interface; the concrete engines
are adapters wired at startup:
  - the native asyncio loop (`core.reasoning.response_gen.ResponseGenerator`), and
  - a LangGraph graph (`adapters.orchestrator.langgraph_orchestrator`).
Swapping engines = wire a different adapter; no `core/` business logic changes.

The interface lives in `core/` (not `ports/`) only because it returns a core
domain type (`GenerationResult`); `ports/` may not import `core`. The invariant
that matters — `core/` never imports a concrete engine/`adapters/` — still holds:
the LangGraph library is imported ONLY inside its adapter.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from core.reasoning.prompt_assembly import AssembledPrompt, DisambiguationRequest
    from core.reasoning.response_gen import GenerationResult, ToolDispatch
    from core.tools.registry import ToolContext


class Orchestrator(Protocol):
    """Runs one conversational turn end-to-end and returns the reply + metadata."""

    async def generate(
        self,
        prompt: AssembledPrompt | DisambiguationRequest,
        dispatcher: ToolDispatch | None = None,
        context: ToolContext | None = None,
    ) -> GenerationResult:
        """Text turn: reason (+ tools) → self-reflect → finalize."""
        ...

    async def generate_spoken(
        self,
        prompt: AssembledPrompt | DisambiguationRequest,
        dispatcher: ToolDispatch | None,
        context: ToolContext | None,
        speak: Callable[[str], Awaitable[None]],
        *,
        temperature: float | None = None,
        flush: Callable[[], Awaitable[None]] | None = None,
    ) -> GenerationResult:
        """Voice turn: same reasoning, streamed to ``speak`` for TTS.

        ``temperature`` overrides the reply temperature for this turn (the open
        greeting and the lull check-in run hotter so they vary session to session).
        It is part of the PORT because the voice edge sets it on every turn — an
        engine that omitted it broke the live path while the text path stayed green.
        """
        ...


class OrchestratorContractError(TypeError):
    """A wired engine cannot accept the call the serving edge makes."""


def assert_orchestrator_contract(engine: Any) -> None:
    """Fail FAST (at wiring time) if ``engine`` can't accept the exact call the voice
    edge makes on every turn — ``voice/session.py::_speak_turn``.

    This exists because that mismatch previously surfaced only at runtime, inside a
    broad ``except Exception``, as silence on every voice turn (F1). Binding the real
    call shape against the real signature turns that into a startup crash.
    """
    for name in ("generate", "generate_spoken"):
        if not callable(getattr(engine, name, None)):
            raise OrchestratorContractError(
                f"{type(engine).__name__} does not implement Orchestrator.{name}()"
            )
    sentinel = object()
    try:
        # The literal call shape of VoiceSession._speak_turn: four positional args
        # (prompt, dispatcher, context, speak) plus the temperature + flush keywords.
        inspect.signature(engine.generate_spoken).bind(
            sentinel, sentinel, sentinel, sentinel, temperature=None, flush=None
        )
    except TypeError as exc:
        raise OrchestratorContractError(
            f"{type(engine).__name__}.generate_spoken() cannot accept the call the voice "
            f"edge makes (prompt, dispatcher, context, speak, temperature=..., flush=...): {exc}"
        ) from exc
