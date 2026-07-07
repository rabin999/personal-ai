"""LangGraph orchestrator (A1): the reasoning turn as an explicit graph.

Implements the `core.reasoning.orchestrator.Orchestrator` port. LangGraph is
imported ONLY here — `core/` depends on the port, never on this library, so the
engine stays swappable (A1.5).

The turn is a real multi-node graph, not a one-pass call:

    perceive → resolve_context (A3) → respond → reflect_log
              (anaphora / working-memory connection)   (proven reasoning core)

Each node writes a structured envelope into the per-turn trace INCLUDING negative
decisions (A5): what the utterance was judged to connect to and why, what context
was pulled in (and left out), which model reasoned, and — from the reasoning core
— tool/reflection outcomes. The heavy, well-tested reasoning/gates/tool-loop live
in `ResponseGenerator` and are orchestrated here as the `respond` node (per the
addendum: "keep the brain's pieces behind the same ports; LangGraph orchestrates
them"), so the graph adds context-connection + deep logging without regressing the
judged response-quality work.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from core.observability.logger import StructuredLogger
from core.reasoning.prompt_assembly import AssembledPrompt, DisambiguationRequest
from core.reasoning.response_gen import GenerationResult, ResponseGenerator, ToolDispatch
from core.tools.registry import ToolContext
from ports.llm import LLM, LLMUnavailable

logger = logging.getLogger(__name__)

_CONTEXT_INSTRUCTIONS = (
    "You are the CONTEXT step of a companion's mind. Look at the recent conversation "
    "and the user's new message, and work out how the new message connects to what "
    "was already said. Resolve references ('that', 'it', 'the second one', 'the "
    "temperature you told me') to the specific earlier thing. Decide: is this a "
    "follow-up to the previous topic, a new topic, a correction, or a continuation?\n"
    'Respond ONLY with JSON: {"relation": "follow_up|new_topic|correction|'
    'continuation", "refers_to": "<the specific earlier thing it refers to, or '
    'empty>", "note": "<one short sentence the responder should know, e.g. \'They '
    "mean the Kathmandu weather you just gave (23C, thunderstorms).'>\"}"
)


class _TurnState(TypedDict, total=False):
    prompt: AssembledPrompt
    dispatcher: ToolDispatch | None
    context: ToolContext | None
    context_note: str
    suppress_search: bool
    result: GenerationResult


class LangGraphOrchestrator:
    """Graph-structured turn engine behind the Orchestrator port."""

    def __init__(
        self,
        llm: LLM,
        generator: ResponseGenerator,
        logs: StructuredLogger | None = None,
    ) -> None:
        self._llm = llm
        self._generator = generator
        self._logs = logs
        self._graph = self._build_graph()

    # ── graph construction ────────────────────────────────────────────────

    def _build_graph(self) -> Any:
        g = StateGraph(_TurnState)
        g.add_node("perceive", self._perceive)
        g.add_node("resolve_context", self._resolve_context)
        g.add_node("respond", self._respond)
        g.add_node("reflect_log", self._reflect_log)
        g.add_edge(START, "perceive")
        g.add_edge("perceive", "resolve_context")
        g.add_edge("resolve_context", "respond")
        g.add_edge("respond", "reflect_log")
        g.add_edge("reflect_log", END)
        return g.compile()

    # ── the Orchestrator port ─────────────────────────────────────────────

    async def generate(
        self,
        prompt: AssembledPrompt | DisambiguationRequest,
        dispatcher: ToolDispatch | None = None,
        context: ToolContext | None = None,
    ) -> GenerationResult:
        if isinstance(prompt, DisambiguationRequest):
            return await self._generator.generate(prompt, dispatcher, context)
        init: _TurnState = {"prompt": prompt, "dispatcher": dispatcher, "context": context}
        state = await self._graph.ainvoke(init)
        result: GenerationResult = state["result"]
        return result

    async def generate_spoken(
        self,
        prompt: AssembledPrompt | DisambiguationRequest,
        dispatcher: ToolDispatch | None,
        context: ToolContext | None,
        speak: Callable[[str], Awaitable[None]],
    ) -> GenerationResult:
        # Voice turn: run the graph (context-resolution included), then speak the
        # reply. The proven streaming path is available via the native engine; the
        # graph keeps the same reasoning and adds the context step.
        result = await self.generate(prompt, dispatcher, context)
        await speak(result.voice_text or result.final_text)
        return result

    # ── nodes ─────────────────────────────────────────────────────────────

    async def _perceive(self, state: _TurnState) -> _TurnState:
        prompt = state["prompt"]
        self._span(
            "reasoning",
            node="perceive",
            utterance=prompt.utterance,
            prompt_version=prompt.prompt_version,
            recent_turns=[m.get("content", "")[:120] for m in prompt.messages[1:-1]][-4:],
        )
        return {}

    async def _resolve_context(self, state: _TurnState) -> _TurnState:
        """A3: reason about how this utterance connects to the conversation and
        resolve references, so a follow-up like 'that temperature' is understood."""
        prompt = state["prompt"]
        history = list(prompt.messages[1:-1])  # drop system + current utterance
        note = ""
        relation = "new_topic"
        if history:  # only worth resolving when there IS prior context
            convo = "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:])
            user_msg = f"Recent conversation:\n{convo}\n\nNew message: {prompt.utterance}"
            messages = [
                {"role": "system", "content": _CONTEXT_INSTRUCTIONS},
                {"role": "user", "content": user_msg},
            ]
            try:
                res = await self._llm.complete(
                    prompt.user_id,
                    messages,
                    "moderate",
                    response_format={"type": "json_object"},
                    session_id=prompt.session_id,
                )
                parsed = json.loads(_strip(res.text))
                relation = str(parsed.get("relation") or "new_topic")
                note = str(parsed.get("note") or "").strip()
                refers_to = str(parsed.get("refers_to") or "").strip()
            except (LLMUnavailable, json.JSONDecodeError, ValueError, KeyError):
                refers_to = ""
        else:
            refers_to = ""
        # A3: if this is a follow-up/continuation whose answer is carried in the
        # conversation (we have a resolution note), suppress the live-info search
        # backstop so a fresh, irrelevant search doesn't override the carried info.
        carried = relation in ("follow_up", "continuation", "correction") and bool(note)
        # A5: log what it connected to and WHY (and that there was nothing to when new).
        self._span(
            "reasoning",
            node="resolve_context",
            relation=relation,
            refers_to=refers_to,
            note=note
            or ("no prior context to connect to" if not history else "no clear reference"),
            suppress_live_search=carried,
        )
        return {"context_note": note, "suppress_search": carried}

    async def _respond(self, state: _TurnState) -> _TurnState:
        """Run the proven reasoning core (judgment → tools → gates → reflection),
        with the resolved context note injected so it uses the right prior context."""
        prompt = state["prompt"]
        dispatcher = state.get("dispatcher")
        context = state.get("context")
        note = state.get("context_note", "")
        suppress = state.get("suppress_search", False)
        turn_prompt = _augment(prompt, note, suppress) if note else prompt
        self._span(
            "reasoning",
            node="respond",
            model_tier=prompt.complexity_hint,
            context_used=bool(note),
            live_search_suppressed=suppress,
        )
        result = await self._generator.generate(turn_prompt, dispatcher, context)
        return {"result": result}

    async def _reflect_log(self, state: _TurnState) -> _TurnState:
        """A5: surface the outcome — action, style flags, and the tool 'why-not'."""
        result = state["result"]
        dispatcher = state.get("dispatcher")
        context = state.get("context")
        tools = []
        if dispatcher is not None and context is not None:
            try:
                tools = [t.id for t in dispatcher.tools_for(context)]
            except Exception:
                tools = []
        # Whether a tool ran is visible on the tool spans; here we record the set
        # that was AVAILABLE and the final action, so a not-called tool is explained
        # by the turn's action (e.g. answered from memory → no web_search needed).
        self._span(
            "reasoning",
            node="reflect_log",
            action=result.action,
            style_flags=result.style_flags,
            available_tools=tools,
        )
        return {}

    # ── helpers ───────────────────────────────────────────────────────────

    def _span(self, stage: str, **fields: Any) -> None:
        if self._logs is not None:
            self._logs.log("info", "graph.node", stage=stage, **fields)


def _augment(prompt: AssembledPrompt, note: str, suppress_search: bool = False) -> AssembledPrompt:
    """Inject the context-resolution note as a system hint before the utterance, and
    flag whether the live-info search backstop should be suppressed (A3)."""
    hint = f"Context: {note}"
    if suppress_search:
        hint += " Answer from this and the conversation above — do NOT search the web again."
    messages = [
        *prompt.messages[:-1],
        {"role": "system", "content": hint},
        prompt.messages[-1],
    ]
    return prompt.model_copy(update={"messages": messages, "suppress_live_search": suppress_search})


def _strip(text: str) -> str:
    t = text.strip()
    if "{" in t and "}" in t:
        return t[t.index("{") : t.rindex("}") + 1]
    return t
