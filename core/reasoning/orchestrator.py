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

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

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
    ) -> GenerationResult:
        """Voice turn: same reasoning, streamed to ``speak`` for TTS."""
        ...
