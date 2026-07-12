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
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from core.observability.logger import StructuredLogger
from core.reasoning.prompt_assembly import AssembledPrompt, DisambiguationRequest
from core.reasoning.prosody import emotion_from_text
from core.reasoning.response_gen import GenerationResult, ResponseGenerator, ToolDispatch
from core.reasoning.volatility import is_volatile_question
from core.tools.registry import ToolContext
from ports.llm import LLM, LLMUnavailable
from ports.prompt import PromptProvider

# A WHOLE-utterance pure greeting / social pleasantry. These are never volatile and carry no
# reference to resolve, so the context/intent verdict is deterministically needs_live_info=False
# — we can skip the ~1.6s LLM call and keep a trivial turn fast. Deliberately NARROW (anchored,
# whole-utterance) so it can never swallow a real question the way the old complexity-hint skip
# did ("who is the current PM of Nepal?" is "simple" by word count — that was D-2).
_TRIVIAL_SOCIAL = re.compile(
    r"^\s*(?:hi+|hey+|hello+|yo|sup|hiya|howdy|heya|"
    r"good\s+(?:morning|afternoon|evening|day)|mornin[g']?|evenin[g']?|"
    r"how(?:'s| is| are| r)?\s+(?:it going|you(?:\s+doing)?|u|things|life)|"
    r"what'?s\s+up|wh?a[sz]+up|"
    r"thank\s*(?:you|s)?(?:\s+(?:so much|a lot))?|thanks|ty|cheers|"
    r"ok(?:ay)?|kk|cool|nice|sweet|awesome|great|got it|gotcha|sounds good|alright|right|"
    r"lol|haha+|hmm+|yeah|yep|yup|nope|nah|"
    r"(?:good\s*)?(?:bye+|night|nite)|see\s+(?:ya|you)(?:\s+later)?|later|take care|"
    r"i'?m\s+(?:good|fine|okay|ok|alright|great)"
    r")(?:\s+(?:there|you|man|buddy|friend|mate|everyone|all))?[\s,!.?…—-]*$",
    re.IGNORECASE,
)


def _is_trivial_social(utterance: str) -> bool:
    """True for a whole-utterance greeting / social pleasantry with nothing to look up or
    resolve — safe to skip the context/intent LLM call. Guarded against any volatile phrasing."""
    return bool(_TRIVIAL_SOCIAL.match(utterance or "")) and not is_volatile_question(
        utterance or ""
    )


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
    "3. LIVE INFO — would a confident answer risk being STALE? Set needs_live_info=true "
    "for ANYTHING whose true answer can change over time, even if you believe you know "
    "it. That includes: who CURRENTLY holds a role or title (prime minister, president, "
    "CEO, champion, manager) and whether they 'still' hold it; prices, rates, scores, "
    "standings; the weather; today's news or 'what's happening'; whether something is "
    "still true/open/alive/available; recent events; any unfamiliar name, ticker or term. "
    "Your training data is old — a role-holder question is ALWAYS needs_live_info=true. "
    "'things at the office are rough' needs NO search (it's emotional); 'what's the "
    "capital of France' needs NO search (stable fact); 'what's 15% of 240' needs NO "
    "search (arithmetic). When in doubt about volatility, prefer true. If "
    "needs_live_info is true you MUST give a concrete live_query. The current date, day, "
    "and UTC time are ALREADY provided to the responder, so a question about today's "
    "date / day-of-week / the current time needs NO search — the responder answers it "
    "directly.\n"
    "4. CONNECTION — how the new message connects to what was said: resolve references "
    "('that', 'the temperature you told me') to the specific earlier thing, and label "
    "the relation.\n"
    'Respond ONLY with JSON: {"intent": "<what they really want, one phrase>", '
    # D-5: this used to read `"<the feeling, or empty>"`, and models complied literally —
    # writing the word "empty", which `emotion_from_text` then parsed as sadness. Name the
    # neutral value explicitly so the prompt and the parser share one vocabulary.
    '"emotional_read": "<the feeling in one word, or \\"neutral\\" if none>", '
    '"needs_live_info": true|false, '
    '"live_query": "<search query if needs_live_info, else empty>", '
    '"relation": "follow_up|new_topic|correction|continuation", '
    '"refers_to": "<the specific earlier thing it refers to, or empty>", '
    '"note": "<one short sentence the responder should know, folding in the intent + '
    "any reference, e.g. 'They mean the Kathmandu weather you just gave (23C); meet "
    "the worry in their voice.'>\"}"
)


@dataclass(frozen=True)
class _Resolution:
    """What the context/intent step concluded about this turn."""

    note: str = ""
    suppress_search: bool = False
    # None = the classifier gave no usable answer; the caller falls back (S1).
    needs_live_info: bool | None = None
    live_query: str = ""
    # C3/S4: the reasoning step's read of the feeling behind the message. Used as the
    # TEXT-SENTIMENT fallback for prosody when acoustic SER is unavailable.
    emotional_read: str = ""


class _TurnState(TypedDict, total=False):
    prompt: AssembledPrompt
    dispatcher: ToolDispatch | None
    context: ToolContext | None
    resolution: _Resolution
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
        *,
        temperature: float | None = None,
        flush: Callable[[], Awaitable[None]] | None = None,
        proactive: bool = False,
    ) -> GenerationResult:
        """Voice turn: run the context-resolution step, then delegate to the proven
        STREAMING generator so TTS starts on the first sentence (low TTFT) — the
        graph adds context-connection without losing streaming latency."""
        if isinstance(prompt, DisambiguationRequest):
            return await self._generator.generate_spoken(
                prompt, dispatcher, context, speak, temperature=temperature, flush=flush
            )
        self._perceive_span(prompt)
        # Skip the ~1.6s context/intent call on a pure greeting/social line (same safe skip as
        # the text node) so a trivial voice turn stays fast; never on a volatile/question turn.
        if _is_trivial_social(prompt.utterance):
            res = _Resolution(needs_live_info=False)
        elif is_volatile_question(prompt.utterance):
            # A clearly-volatile question (role-holder / latest / current-events) ALWAYS needs
            # live info — the classifier would only confirm what the fast check already knows.
            # Skipping it here lets the instant interjection fire ~1.6s sooner so the first
            # spoken chunk lands inside the 3-5s target (user: "chunks not under 3-5s"). The
            # engine builds the query from the utterance when there's no classifier live_query.
            res = _Resolution(needs_live_info=True)
        else:
            res = await self._resolve_note(prompt)
        # S1: ALWAYS augment — the volatility verdict must reach the reasoning core even
        # when there is no context note to inject. Previously `if note else prompt` threw
        # `needs_live_info` away on exactly the turns that had no prior context.
        turn_prompt = _augment(prompt, res)
        self._span(
            "reasoning",
            node="respond",
            streaming=True,
            context_used=bool(res.note),
            needs_live_info=res.needs_live_info,
        )
        result = await self._generator.generate_spoken(
            turn_prompt,
            dispatcher,
            context,
            speak,
            temperature=temperature,
            flush=flush,
            proactive=proactive,
        )
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
        """The context/intent step. Runs on EVERY turn, exactly as `generate_spoken` runs it.

        **D-2.** This node used to skip the `context_intent` call whenever
        `complexity_hint == "simple"` — an L3 latency optimisation. `generate_spoken` calls
        `_resolve_note` unconditionally and never took the shortcut, so the two entrypoints
        disagreed about what the engine had decided. Measured: `needs_live_info` was `None` on
        21 of 21 text turns, and 170 of the 174 questions in `tests/labeled/volatility.jsonl`
        are "simple" under the word-count heuristic — including "who is the current prime
        minister of Nepal?".

        The verdict is not a nicety. Three behaviours read it, and all three were dead on the
        text path: the honest "I couldn't reach it" lines (`_SEARCH_FAILED_TEXT` /
        `_NOT_FOUND_TEXT`, both guarded by `needs_live_info is True`), `suppress_live_search`,
        and the emotional read that selects the delivery register and now gates the tool reflex
        (D-14). A turn whose classifier never ran cannot be said to have a verdict at all.

        The shortcut is deleted rather than duplicated into the voice path: symmetry is the
        invariant, and the cost is one `simple`-tier LLM call on a greeting. Latency work
        belongs behind a cache or a cheaper model, not behind a caller-dependent skip.

        The ONE safe exception: a whole-utterance greeting/social pleasantry ("hi", "thanks",
        "how are you"). It is never volatile and has no reference to resolve, so the verdict is
        deterministically needs_live_info=False — we skip the ~1.6s call and keep it fast. This
        is NOT the D-2 shortcut: that keyed on the broad "simple" word-count hint (which matched
        "who is the current PM of Nepal?"); this matches only a closed set of social phrases and
        is double-guarded against any volatile phrasing.
        """
        prompt = state["prompt"]
        if _is_trivial_social(prompt.utterance):
            if self._logs is not None:
                self._logs.log(
                    "info", "context_intent", stage="resolve_context", skipped="trivial_social"
                )
            return {"resolution": _Resolution(needs_live_info=False)}
        return {"resolution": await self._resolve_note(prompt)}

    async def _resolve_note(self, prompt: AssembledPrompt) -> _Resolution:
        """A3 + F5 + S1: reason about how this utterance connects to the conversation,
        infer the underlying intent behind indirect phrasing, AND decide whether a
        confident answer would risk being stale (`needs_live_info` + `live_query`).

        That last verdict used to be computed here, logged to the trace, and then thrown
        away — routing hung off a phrasing regex instead, which answered "who is the
        current prime minister of Nepal?" from training data. It is now carried onto the
        prompt (S1).
        """
        history = list(prompt.messages[1:-1])  # drop system + current utterance
        relation = "new_topic"
        # `None` = the classifier never gave a usable answer. Distinct from False: the
        # caller must fall back to the regex backstop rather than assume "no search".
        # A JSONDecodeError used to be swallowed straight into False.
        needs_live_info: bool | None = None
        live_query = ""
        # Anchor "current" to the user's own clock, not the model's training cutoff.
        now = datetime.now(UTC)
        anchor = f"(Today is {now.strftime('%A, %d %B %Y')}. 'Current' means THIS date.)"
        if history:
            convo = "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:])
            user_msg = f"{anchor}\nRecent conversation:\n{convo}\n\nNew message: {prompt.utterance}"
        else:  # first turn: no history, but still infer intent from the message alone
            user_msg = f"{anchor}\nNew message (start of conversation): {prompt.utterance}"
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
        # S1: the provider intermittently returns a bare "{" with output_tokens=0
        # (measured: 1 in 12 calls). That used to be swallowed into needs_live_info=False,
        # i.e. "answer from training data". Retry once, then leave the verdict UNKNOWN.
        note = refers_to = intent = emotional_read = ""
        for attempt in range(2):
            try:
                res = await self._llm.complete(
                    prompt.user_id,
                    messages,
                    # Retry on a STRONGER tier: the failure is the provider returning a
                    # bare "{" with output_tokens=0, and re-hitting the same model just
                    # fails again. Costs nothing on the ~5-in-6 happy path.
                    "moderate" if attempt == 0 else "complex",
                    response_format={"type": "json_object"},
                    session_id=prompt.session_id,
                    temperature=0.2,  # P2: routing/intent is a decision → low temp
                    reasoning={"enabled": False},  # P4: no chain-of-thought for routing
                    purpose="context_intent",
                )
                parsed = json.loads(_strip(res.text))
            except LLMUnavailable:
                break  # provider down — the regex backstop decides
            except (json.JSONDecodeError, ValueError, KeyError):
                logger.warning(
                    "context_intent returned unusable JSON (attempt %d): %.80r",
                    attempt + 1,
                    getattr(locals().get("res"), "text", ""),
                )
                continue
            relation = str(parsed.get("relation") or "new_topic")
            note = str(parsed.get("note") or "").strip()
            refers_to = str(parsed.get("refers_to") or "").strip()
            intent = str(parsed.get("intent") or "").strip()
            emotional_read = str(parsed.get("emotional_read") or "").strip()
            needs_live_info = bool(parsed.get("needs_live_info"))
            live_query = str(parsed.get("live_query") or "").strip()
            break
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
        return _Resolution(
            note=note,
            suppress_search=carried,
            needs_live_info=needs_live_info,
            live_query=live_query,
            emotional_read=emotional_read,
        )

    async def _respond(self, state: _TurnState) -> _TurnState:
        """Run the proven reasoning core (judgment → tools → gates → reflection),
        with the resolved context note injected so it uses the right prior context."""
        prompt = state["prompt"]
        dispatcher = state.get("dispatcher")
        context = state.get("context")
        res = state.get("resolution") or _Resolution()
        turn_prompt = _augment(prompt, res)
        self._span(
            "reasoning",
            node="respond",
            model_tier=prompt.complexity_hint,
            context_used=bool(res.note),
            live_search_suppressed=res.suppress_search,
            needs_live_info=res.needs_live_info,
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


def _augment(prompt: AssembledPrompt, res: _Resolution) -> AssembledPrompt:
    """Carry the context/intent step's conclusions onto the prompt.

    The note becomes a system hint before the utterance (A3). The volatility verdict
    (`needs_live_info` / `live_query`) rides on the prompt itself so the reasoning core
    can route on it instead of on a phrasing regex (S1).
    """
    update: dict[str, Any] = {
        "suppress_live_search": res.suppress_search,
        "needs_live_info": res.needs_live_info,
        "live_query": res.live_query,
    }
    # C3: acoustic SER wins when present; otherwise fall back to the text-sentiment read
    # the reasoning step already produced. Without this `prompt.emotion` is always None in
    # every real deployment (`ser_service_url` is empty), so the register is always
    # "neutral" and the dynamic-prosody system never executes.
    if not prompt.emotion:
        derived = emotion_from_text(res.emotional_read)
        if derived is not None:
            update["emotion"] = derived
    if res.note:
        hint = f"Context: {res.note}"
        if res.suppress_search:
            hint += " Answer from this and the conversation above — do NOT search the web again."
        update["messages"] = [
            *prompt.messages[:-1],
            {"role": "system", "content": hint},
            prompt.messages[-1],
        ]
    return prompt.model_copy(update=update)


def _strip(text: str) -> str:
    t = text.strip()
    if "{" in t and "}" in t:
        return t[t.index("{") : t.rindex("}") + 1]
    return t
