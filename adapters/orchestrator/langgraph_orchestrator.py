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

import asyncio
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
from ports.prompt import PromptProvider

logger = logging.getLogger(__name__)

_CONTEXT_INSTRUCTIONS = (
    "You are the CONTEXT + INTENT step of a companion's mind. Look at the recent "
    "conversation (if any) and the user's new message, and work out:\n"
    "1. INTENT — what the user is REALLY trying to get from you, especially when they "
    "ask INDIRECTLY. E.g. 'what's happening in Nepal really gives me pain' implies "
    "they want you to KNOW the current events in Nepal AND to meet the emotional "
    "weight — not to ask 'what do you mean?'. Infer the underlying want.\n"
    "2. EMOTIONAL READ — the feeling behind it, if any (pain, excitement, stress), or "
    "empty if neutral.\n"
    "3. LIVE INFO — does answering well need CURRENT, real-world info the model can't "
    "be sure of (news, scores, weather, prices, 'what's happening', an unfamiliar "
    "name/term)? If so, give the search query. 'things at the office are rough' needs "
    "NO search (it's emotional); 'that match last night' DOES (the result). The "
    "current date, day, and UTC time are ALREADY provided to the responder, so a "
    "question about today's date / day-of-week / the current time needs NO search "
    "(needs_live_info=false) — the responder answers it directly.\n"
    "4. CONNECTION — how the new message connects to what was said: resolve references "
    "('that', 'the temperature you told me') to the specific earlier thing, and label "
    "the relation.\n"
    'Respond ONLY with JSON: {"intent": "<what they really want, one phrase>", '
    '"emotional_read": "<the feeling, or empty>", "needs_live_info": true|false, '
    '"live_query": "<search query if needs_live_info, else empty>", '
    '"relation": "follow_up|new_topic|correction|continuation", '
    '"refers_to": "<the specific earlier thing it refers to, or empty>", '
    '"note": "<one short sentence the responder should know, folding in the intent + '
    "any reference, e.g. 'They mean the Kathmandu weather you just gave (23C); meet "
    "the worry in their voice.'>\"}"
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
        prompts: PromptProvider | None = None,
    ) -> None:
        self._llm = llm
        self._generator = generator
        self._logs = logs
        self._prompts = prompts  # F13: fetch the context/intent prompt at runtime
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
        """Voice turn: run the context-resolution step, then delegate to the proven
        STREAMING generator so TTS starts on the first sentence (low TTFT) — the
        graph adds context-connection without losing streaming latency."""
        if isinstance(prompt, DisambiguationRequest):
            return await self._generator.generate_spoken(prompt, dispatcher, context, speak)
        self._perceive_span(prompt)
        note, suppress = await self._resolve_note(prompt)
        turn_prompt = _augment(prompt, note, suppress) if note else prompt
        self._span("reasoning", node="respond", streaming=True, context_used=bool(note))
        result = await self._generator.generate_spoken(turn_prompt, dispatcher, context, speak)
        self._reflect_span(prompt, result, dispatcher, context)
        return result

    # ── nodes ─────────────────────────────────────────────────────────────

    async def _perceive(self, state: _TurnState) -> _TurnState:
        self._perceive_span(state["prompt"])
        return {}

    def _perceive_span(self, prompt: AssembledPrompt) -> None:
        # A5: log the persona/profile read the agent has for this user this turn —
        # the emotional read + which soft-signal/preference context is in play.
        sections = prompt.sections
        persona_active = [
            k
            for k in ("psych", "preferences", "self_statements", "facts")
            if sections.get(k, "").strip()
        ]
        self._span(
            "reasoning",
            node="perceive",
            utterance=prompt.utterance,
            prompt_version=prompt.prompt_version,
            emotion=prompt.emotion,
            persona_context=persona_active,
            recent_turns=[m.get("content", "")[:120] for m in prompt.messages[1:-1]][-4:],
        )

    async def _resolve_context(self, state: _TurnState) -> _TurnState:
        note, suppress = await self._resolve_note(state["prompt"])
        return {"context_note": note, "suppress_search": suppress}

    async def _resolve_note(self, prompt: AssembledPrompt) -> tuple[str, bool]:
        """A3 + F5: reason about how this utterance connects to the conversation AND
        infer the underlying intent behind indirect phrasing (what they really want,
        the emotional weight, whether current info is needed). Runs every turn — even
        the first, so an indirect first message ('what's happening in Nepal gives me
        pain') gets its intent inferred and logged. Returns (note, suppress_live_search)
        and logs the inferred intent + why in the trace (F5/F7)."""
        history = list(prompt.messages[1:-1])  # drop system + current utterance
        note = ""
        relation = "new_topic"
        refers_to = ""
        intent = ""
        emotional_read = ""
        needs_live_info = False
        live_query = ""
        if history:
            convo = "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:])
            user_msg = f"Recent conversation:\n{convo}\n\nNew message: {prompt.utterance}"
        else:  # first turn: no history, but still infer intent from the message alone
            user_msg = f"New message (start of conversation): {prompt.utterance}"
        # F13: fetch the CONTEXT+INTENT system prompt from prompt management
        # (Langfuse) at runtime so it's versioned/editable without a code change;
        # falls back to the bundled default if unavailable. Record which version ran.
        instructions = _CONTEXT_INSTRUCTIONS
        prompt_name = prompt_version_id = prompt_source = ""
        if self._prompts is not None:
            try:
                rendered = await asyncio.to_thread(self._prompts.get, "context_intent")
                if rendered.text.strip():
                    instructions = rendered.text
                prompt_name, prompt_version_id, prompt_source = (
                    rendered.name,
                    str(rendered.version),
                    rendered.source,
                )
            except Exception:
                pass
        messages = [
            {"role": "system", "content": instructions},
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
            intent = str(parsed.get("intent") or "").strip()
            emotional_read = str(parsed.get("emotional_read") or "").strip()
            needs_live_info = bool(parsed.get("needs_live_info"))
            live_query = str(parsed.get("live_query") or "").strip()
        except (LLMUnavailable, json.JSONDecodeError, ValueError, KeyError):
            refers_to = ""
        # A3: a follow-up/continuation/correction whose answer is carried → suppress
        # the live-info search backstop so a fresh, irrelevant search can't override.
        # But NEVER suppress when this turn genuinely needs current info (F5): an
        # indirect ask about current events must still be allowed to search.
        carried = (
            relation in ("follow_up", "continuation", "correction")
            and bool(note)
            and not needs_live_info
        )
        self._span(
            "reasoning",
            node="resolve_context",
            relation=relation,
            refers_to=refers_to,
            intent=intent,  # F5: the inferred underlying intent
            emotional_read=emotional_read,  # F5: the emotional weight read
            needs_live_info=needs_live_info,  # F5: was current info judged necessary
            live_query=live_query,  # F5: the search the intent implies
            note=note
            or ("no prior context to connect to" if not history else "no clear reference"),
            suppress_live_search=carried,
            # F13: which managed prompt (name + version + source) drove this step,
            # so a prompt-version change in Langfuse is visible in the very next turn.
            prompt_name=prompt_name,
            prompt_managed_version=prompt_version_id,
            prompt_source=prompt_source,
        )
        return note, carried

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
        self._reflect_span(
            state["prompt"], state["result"], state.get("dispatcher"), state.get("context")
        )
        return {}

    def _reflect_span(
        self,
        prompt: AssembledPrompt,
        result: GenerationResult,
        dispatcher: ToolDispatch | None,
        context: ToolContext | None,
    ) -> None:
        """A5: surface the outcome — action, style flags, and an explicit tool
        'why-not' for every available tool that did NOT run this turn."""
        tools: list[str] = []
        if dispatcher is not None and context is not None:
            try:
                tools = [t.id for t in dispatcher.tools_for(context)]
            except Exception:
                tools = []
        why_not = {}
        if result.action == "respond":
            for tid in tools:
                if tid == "web_search":
                    why_not[tid] = (
                        "answer carried in context — no live search"
                        if prompt.suppress_live_search
                        else "the model judged no live lookup was needed this turn"
                    )
                else:
                    why_not[tid] = "not needed for this turn's intent"
        self._span(
            "reasoning",
            node="reflect_log",
            action=result.action,
            style_flags=result.style_flags,
            available_tools=tools,
            tool_why_not=why_not,  # A5: explained negatives
        )

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
