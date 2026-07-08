"""Response Generation & Behavior Gates (spec §12).

One LLM call returns a draft + judgment block (Pydantic-validated; retry
once, then safe fallback — never trust unvalidated model JSON, §0.5). The
gates then decide the action:

- curiosity gate (rule 2): clarify / curious follow-up / direct response,
  thresholds from the curiosity_policy trait params (§2)
- overclaim rewrite (rule 3): §9 check_boundary before anything leaves
- pull-based disclosure (rule 4): one honest sentence only when the user's
  intent asks about the system's nature — never volunteered

Gate thresholds, disclosure wording, and question phrasing are mechanism
defaults — final feel is tuned by a human (contract §7).
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, BeforeValidator, ValidationError

from core.observability.logger import StructuredLogger
from core.profile import ProfileNotFound, TraitRegistry
from core.reasoning.prompt_assembly import AssembledPrompt, DisambiguationRequest
from core.reasoning.self_model import BoundaryFlag, SelfModel, TurnRecord
from core.reasoning.style import find_forbidden, scrub_forbidden
from core.tools.dispatcher import ConfirmRequest, QueuedHandle, ToolCall, ToolResult
from core.tools.registry import ToolContext, ToolSpec, UnknownTool
from ports.llm import LLM, LLMUnavailable, Tier

logger = logging.getLogger(__name__)

Action = Literal["respond", "clarify", "curious_followup", "disambiguate"]

# Agentic tool loop (§8/§14.11): max tool round-trips before the model must answer.
MAX_TOOL_STEPS = 4


class _CostBudget:
    """Per-turn spend accumulator for the cost ceiling (§10). Bounds a runaway
    tool/reasoning loop by cost, on top of the fixed step cap."""

    def __init__(self, cap_usd: float) -> None:
        self.cap = cap_usd
        self.spent = 0.0

    def add(self, usd: float) -> None:
        self.spent += usd or 0.0

    def exceeded(self) -> bool:
        return self.cap > 0 and self.spent >= self.cap


# On a JSON-validation retry, escalate one tier: the cheap/fast model (e.g.
# gemini-flash-lite) intermittently returns malformed JSON even with
# response_format, and re-hitting the SAME model just fails again → a generic
# fallback on a real turn. A stronger model produces valid JSON, so the retry is
# not wasted. The happy path (valid JSON first try) is unchanged.
_ESCALATE_TIER: dict[Tier, Tier] = {
    "simple": "moderate",
    "moderate": "complex",
    "complex": "complex",
}


class ToolDispatch(Protocol):
    """The slice of the §13 dispatcher the response loop needs."""

    def tools_for(self, context: ToolContext) -> list[ToolSpec]: ...

    async def dispatch(
        self, call: ToolCall, context: ToolContext, *, confirmed: bool = False
    ) -> Any: ...

    async def run_inline(self, call: ToolCall, context: ToolContext) -> Any: ...


# Default gate thresholds; overridden by curiosity_policy trait params. Tuned to
# strongly favor responding over clarifying — intent inference is the priority, so
# we only clarify when the model is very unsure (low T_intent) or ambiguity is
# extreme (high T_ambig).
DEFAULT_GATE_PARAMS = {"T_intent": 0.3, "T_novel": 0.75, "T_emotion": 0.7, "T_ambig": 0.85}

# §5-mandated safety net: only used when the LLM's JSON fails validation twice
# (rare provider/parse glitch). It must still sound like the companion — a warm,
# present line — NOT a service-desk "what do you mean?" clarifier (that banned
# shape is exactly what a greeting like "hi there" used to fall back to). The real
# disclosure (rule 4) and background acks are model-generated in-voice, never here.
_SAFE_FALLBACK_TEXT = "Hey, I'm right here with you — what's going on?"

# Confirmation resolution (§8.3): cheap lexical yes/no on a pending action.
_AFFIRMATIVE = (
    "yes",
    "yeah",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "go ahead",
    "do it",
    "please do",
    "confirm",
    "sounds good",
    "go for it",
)
_NEGATIVE = (
    "no",
    "nope",
    "nah",
    "don't",
    "do not",
    "cancel",
    "skip",
    "stop",
    "never mind",
    "nevermind",
    "forget it",
)

# NOTE: field ORDER matters — judgment + tool_request come BEFORE draft_response
# so the streaming voice path (§8.12) can read the tool decision before it starts
# speaking the reply, and only stream when no tool is needed.
_JUDGMENT_INSTRUCTIONS = """
Respond ONLY with a JSON object of this exact shape, with the keys in THIS ORDER:
{"judgment": {"intent_confidence": <0..1 how sure you are of what the user wants>,
              "novelty_score": <0..1 how new this topic is for this user>,
              "emotional_salience": <0..1 emotional weight of this moment>,
              "ambiguity": <0..1 how ambiguous the request is>,
              "complexity_tier": "simple|moderate|complex",
              "requires_nature_disclosure": <true ONLY if the user is DIRECTLY
                 asking about YOUR nature in a way that needs an honest "I'm an AI"
                 to answer truthfully — e.g. "do you actually care?", "are you
                 human/real/a bot?", "would you miss me?", "do you have feelings?".
                 It is FALSE for philosophical or values questions that just happen
                 to include the word "you" — "what makes life meaningful?", "what
                 is happiness?", "do you ever think about death?" — those you engage
                 warmly as a friend WITHOUT any AI-disclaimer. False for everything
                 else; never volunteer it. When true, fold a brief, natural, honest
                 one-sentence acknowledgement that you're an AI INTO your
                 draft_response in your own warm voice, then keep talking with them —
                 never a canned or ToS-style disclaimer, never "my existence is to
                 assist" or "I don't have feelings/consciousness">,
              "capability_boundary_flag": null | "overclaim_empathy" | "overclaim_consciousness"},
 "tool_request": null | {"tool_id": "<id>", "args": {<per the tool's schema>}},
 "draft_response": "<your reply — short, warm, natural spoken language. The voice
   ACTUALLY performs inline delivery tags, so WEAVE THEM IN to sound human, not
   flat: [laugh] [chuckle] [sigh] [gasp] for feeling; [warm] [gentle] [soft] for
   tone; <emphasis>word</emphasis> to stress a word; <slow> ... </slow> to slow
   down; <pause> for a beat. Use 1-3 tags where they genuinely fit the moment
   (a laugh when something's funny, a gentle tone when they're down) — never tag
   every sentence, never force it. Example: 'Oh [laugh] that's amazing — <emphasis>
   congrats</emphasis>!'>"}
Set capability_boundary_flag if your draft claims felt emotion or consciousness.
tool_request: leave null unless you need a tool to answer well (see tools below).
Ground every factual claim about the user in the conversation and the provided
memories/facts. If the answer is not in your context, say you don't remember —
NEVER invent details about the user's life.
""".strip()


# Plain-text spoken reply for the streaming voice path (§8.12) — no JSON, so it
# streams from the first token straight into TTS. Judgment/gates are skipped for
# these plain conversational turns (they always just respond).
_SPOKEN_REPLY_INSTRUCTIONS = """
Reply out loud in your own warm, natural voice — 1-3 short spoken sentences, like
a close friend actually talking. Work out what they really mean and respond to
THAT; make a sensible best-effort read of their intent and never stall with 'what
do you mean?' / 'what are you talking about?'. Weave in 1-3 inline delivery tags
where they genuinely fit so the voice sounds human, not flat: [laugh] [chuckle]
[sigh] for feeling; [warm] [gentle] [soft] for tone; <emphasis>word</emphasis> to
stress a word; <pause> for a beat — never tag every sentence. If they ask whether
you're real, an AI, or whether you have feelings, be honest about being an AI in
one short warm sentence folded into your reply — never a canned disclaimer. Never
use assistant / service-desk phrasing. Reply with ONLY the spoken words — no JSON,
no quotes, no preamble.
""".strip()


def _clamp_unit(value: Any) -> float:
    """Models sometimes emit 1.2 or "0.8"; clamp instead of rejecting the turn."""
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _coerce_flag(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in ("null", "none", ""):
        return None
    return value


def _coerce_bool(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return value


def _coerce_tool_request(value: Any) -> Any:
    # Models emit "null"/{}/{"tool_id":""} for "no tool" — treat all as None.
    if not isinstance(value, dict) or not str(value.get("tool_id") or "").strip():
        return None
    return value


def _coerce_tier(value: Any) -> Any:
    lowered = str(value).strip().lower()
    return lowered if lowered in ("simple", "moderate", "complex") else "moderate"


UnitScore = Annotated[float, BeforeValidator(_clamp_unit)]


class Judgment(BaseModel):
    intent_confidence: UnitScore = 0.5
    novelty_score: UnitScore = 0.5
    emotional_salience: UnitScore = 0.5
    ambiguity: UnitScore = 0.5
    complexity_tier: Annotated[Tier, BeforeValidator(_coerce_tier)] = "moderate"
    requires_nature_disclosure: Annotated[bool, BeforeValidator(_coerce_bool)] = False
    capability_boundary_flag: Annotated[BoundaryFlag, BeforeValidator(_coerce_flag)] = None


class ToolRequest(BaseModel):
    tool_id: str
    args: dict[str, Any] = {}


class LLMTurn(BaseModel):
    draft_response: str
    judgment: Judgment
    # If set, the model wants a tool run before answering (§8/§14.11 agentic loop).
    tool_request: Annotated[ToolRequest | None, BeforeValidator(_coerce_tool_request)] = None


class GenerationResult(BaseModel):
    # User-facing reply with ALL inline delivery tags stripped (brief §1.4): the
    # chat UI and stored memory get clean prose — tags never appear as text.
    final_text: str
    # The same reply WITH whitelisted delivery tags intact — fed to TTS so the
    # voice actually performs the prosody, and shown raw in the trace (§1.4/§5.10).
    voice_text: str = ""
    action: Action
    judgment: Judgment | None = None
    turn_id: str | None = None
    # Labels for any forbidden assistant-speak found in the final text (§7). The
    # mechanism only FLAGS (wording is human-tuned); the runtime logs it to the
    # trace so a tone regression is visible instead of shipping silently.
    style_flags: list[str] = []


# Self-reflection rewrite (brief §9.3): generic, tone-neutral instruction — it
# only removes the *shape* of assistant-speak, it does not dictate wording, so
# the human still owns tone (§7). Config-gated via ``self_reflect``.
_REWRITE_INSTRUCTIONS = (
    "Re-say the following line in your own warm, natural voice as a close friend who "
    "knows this person — same intent and content, but WITHOUT any service-desk or "
    "assistant phrasing (no 'how can I help you', 'what's on your mind', 'I'm here to "
    "assist', no bolted-on disclaimers). Keep it to one or two spoken sentences. "
    "Reply with ONLY the rewritten line, no quotes, no preamble.\n\nLine: "
)

# Warm-disclosure rewrite (§1.2 rule 4): a nature question ("do you actually
# care?") is emotionally vulnerable, and weaker models often answer it COLDLY
# ("caring isn't really my thing", "I'm just here to crunch the numbers"). When a
# turn requires disclosure we always warm-polish it on a stronger tier: keep the
# honest one-line "I'm an AI", but LEAD with genuine attention/investment.
_DISCLOSURE_REWRITE_INSTRUCTIONS = (
    "Someone just asked a vulnerable question about whether you, their companion, "
    "actually care / are real. Answer as their warm friend. You must be honest that "
    "you're an AI in ONE short sentence, but LEAD with the genuine part: you really "
    "do pay attention to them and they matter to you; being an AI means it isn't the "
    "same as how they feel it, but that does not make your attention fake. NEVER say "
    "'caring isn't my thing', 'I don't do feelings', 'I'm just here to help/crunch "
    "numbers/get you what you need', or anything cold or transactional. Warm, "
    "present, 1-2 spoken sentences. Reply with ONLY the line, no quotes.\n\n"
    "Their question: {q}\nYour draft: {draft}"
)

# A valid disclosure still names the AI nature honestly — guards the warm rewrite
# from silently dropping it.
_HAS_DISCLOSURE = re.compile(
    r"\ban ai\b|\bi'?m an ai\b|\bi am an ai\b|\bnot (a )?(real )?(human|person)\b|\ba bot\b",
    re.IGNORECASE,
)


class ResponseGenerator:
    def __init__(
        self,
        llm: LLM,
        self_model: SelfModel,
        registry: TraitRegistry,
        *,
        self_reflect: bool = True,
        logs: StructuredLogger | None = None,
        max_turn_cost_usd: float = 0.50,
        reasoning_tier: Tier = "complex",
    ) -> None:
        self._llm = llm
        self._self_model = self_model
        self._registry = registry
        self._self_reflect = self_reflect
        self._logs = logs
        self._max_turn_cost_usd = max_turn_cost_usd
        # A2: the main user-facing reasoning turn runs on this (mature) tier, not the
        # flashy fast tier — quality of thought over speed.
        self._reasoning_tier = reasoning_tier
        # Action tools awaiting the user's yes/no, keyed by session (§8.3).
        self._pending: dict[str, ConfirmRequest] = {}

    async def generate(
        self,
        prompt: AssembledPrompt | DisambiguationRequest,
        dispatcher: "ToolDispatch | None" = None,
        context: "ToolContext | None" = None,
    ) -> GenerationResult:
        """One agentic turn (§14.11): the LLM answers, OR requests a tool that is
        dispatched per §8 (readonly inline / background enqueue / action confirm),
        looping until a direct answer — then the behavior gates run."""
        if isinstance(prompt, DisambiguationRequest):
            return await self._disambiguate(prompt)

        can_use_tools = dispatcher is not None and context is not None
        tool_notes: list[str] = []
        # §8.3: if an action tool is awaiting confirmation, this turn is the yes/no.
        # The outcome seeds the generation so the LLM phrases it naturally (no
        # static "Done"/"skipped" strings).
        if can_use_tools and prompt.session_id in self._pending:
            seed = await self._resolve_confirmation(prompt, dispatcher, context)  # type: ignore[arg-type]
            if seed is not None:
                tool_notes = [seed]

        # Action tools that write must run at most ONCE per turn: the model can
        # re-request the same write with jittered args (logging a trade 3-4x), so
        # dedup those by id. Read/background tools dedup by id+args (queries differ).
        action_ids: set[str] = set()
        if can_use_tools and dispatcher is not None and context is not None:
            action_ids = {t.id for t in dispatcher.tools_for(context) if t.type == "action"}

        last_draft = ""
        turn: LLMTurn | None = None
        seen_calls: set[str] = set()  # dedup identical tool calls within the turn
        budget = _CostBudget(self._max_turn_cost_usd)
        for _ in range(MAX_TOOL_STEPS if can_use_tools else 1):
            # Cost ceiling (§10): a runaway loop stops at the configurable cap and
            # answers with what it has, rather than burning the budget.
            if budget.exceeded():
                self._span(
                    "cost_ceiling",
                    level="warn",
                    spent_usd=round(budget.spent, 4),
                    cap_usd=self._max_turn_cost_usd,
                )
                if last_draft.strip():
                    break
                return await self._finish(prompt, _SAFE_FALLBACK_TEXT, "respond", judgment=None)
            turn = await self._call_llm(prompt, dispatcher, context, tool_notes, budget)
            if turn is None:  # both attempts failed validation / provider down
                # Prefer the model's own last words (e.g. its ack when it kicked
                # off a search) over any canned line; only a total outage falls
                # back to the minimal safe reply.
                if last_draft.strip():
                    return await self._finish(prompt, _sanitize_tags(last_draft), "respond", None)
                # The structured JSON path failed — but a PLAIN warm reply (no JSON)
                # is far more robust for any model, and salvages the turn's real
                # content (e.g. celebrating a promotion) instead of a canned line.
                plain = await self._plain_reply(prompt)
                if plain.strip():
                    return await self._finish(prompt, _sanitize_tags(plain), "respond", None)
                # Warm presence, not a clarify — a parse glitch must not make the
                # companion interrogate the user ("what do you mean?").
                return await self._finish(prompt, _SAFE_FALLBACK_TEXT, "respond", judgment=None)
            last_draft = turn.draft_response
            if turn.tool_request is None or not can_use_tools:
                break
            # An action tool runs at most once per turn (dedup by id, since the
            # model jitters args); read/background dedup by id+args.
            tid = turn.tool_request.tool_id
            if tid in action_ids:
                call_key = tid
            else:
                call_key = f"{tid}:{json.dumps(turn.tool_request.args, sort_keys=True)}"
            if call_key in seen_calls:
                tool_notes.append(
                    f"(you already ran '{turn.tool_request.tool_id}' with those exact "
                    "arguments this turn — do NOT call it again; answer the user now)"
                )
                continue
            seen_calls.add(call_key)
            note = await self._dispatch_tool(dispatcher, context, turn.tool_request)  # type: ignore[arg-type]
            if isinstance(note, ConfirmRequest):  # §8.3: action needs confirmation
                self._pending[prompt.session_id] = note  # resolved next turn
                tool_notes.append(
                    f"(the action '{note.tool_id}' needs the user's OK before it runs — ask "
                    "them naturally whether to go ahead; do NOT call any tool this turn)"
                )
                continue  # let the model phrase the confirmation question in-voice
            tool_notes.append(note)
        assert turn is not None
        # Capability backstop (brief §8.8): when the model ran no tool but the turn
        # needs live info — either the user's query is about the current world
        # (weather/news/time/price…) or the draft is a false refusal ("can't access
        # real-time…") / hollow promise ("just a moment while I get those") — run a
        # real web_search and answer in-turn. The companion never says it "can't"
        # or guesses when a search could ground the answer.
        needs_search = (
            can_use_tools
            and not seen_calls
            and not prompt.suppress_live_search  # A3: answer carried in context → no re-search
            and (
                _is_live_info_query(prompt.utterance)
                or _needs_capability_repair(turn.draft_response)
            )
        )
        if needs_search:
            repaired = await self._capability_repair(prompt, dispatcher, context)  # type: ignore[arg-type]
            if repaired:
                turn.draft_response = repaired
        return await self._finalize(prompt, turn)

    async def _capability_repair(
        self, prompt: AssembledPrompt, dispatcher: "ToolDispatch", context: "ToolContext"
    ) -> str | None:
        """Force a real web_search for a live-info/unknown query the model tried to
        refuse, then re-answer with the result (brief §8.8/§8.11). Inline + bounded
        so it answers this turn; the background/waiter path stays for voice latency."""
        if not any(t.id == "web_search" for t in dispatcher.tools_for(context)):
            return None
        try:
            result = await dispatcher.run_inline(
                ToolCall(tool_id="web_search", args={"query": prompt.utterance}), context
            )
        except Exception as exc:  # degrade gracefully — keep the model's own words
            logger.warning("capability-repair search failed: %s", exc)
            return None
        output = getattr(result, "output", {}) or {}
        summary = str(output.get("summary") or "").strip()
        if not summary or not output.get("found"):
            return None
        self._span("tool", tool="web_search", mode="capability_repair", result=summary[:300])
        try:
            completion = await self._llm.complete(
                prompt.user_id,
                [
                    {"role": "system", "content": _REPAIR_INSTRUCTIONS + summary},
                    {"role": "user", "content": prompt.utterance},
                ],
                "simple",
                session_id=prompt.session_id,
            )
        except LLMUnavailable:
            return summary  # at least hand them the real facts
        answer = _sanitize_tags(_strip_fences(completion.text)).strip().strip('"')
        return answer or summary

    async def generate_spoken(
        self,
        prompt: "AssembledPrompt | DisambiguationRequest",
        dispatcher: "ToolDispatch | None",
        context: "ToolContext | None",
        speak: "Callable[[str], Awaitable[None]]",
    ) -> GenerationResult:
        """Voice turn (§8.12): stream the spoken reply to ``speak`` sentence-by-
        sentence so TTS starts on the first sentence, when it's a plain
        conversational turn. Falls back to the full non-streamed path (tool loop,
        gates, capability search) for anything else, then speaks the reply once.
        Always returns the final GenerationResult for memory/trace.
        """
        if isinstance(prompt, DisambiguationRequest):
            result = await self._disambiguate(prompt)
            await speak(result.voice_text or result.final_text)
            return result

        can_use_tools = dispatcher is not None and context is not None
        # Stream only a plain reply: no pending confirmation, and not a live-info
        # query (those need a tool/search first). Tool turns and refusals go the
        # full path so we never speak a holding line then re-answer.
        streamable = not (
            can_use_tools and prompt.session_id in self._pending
        ) and not _is_live_info_query(prompt.utterance)
        if streamable:
            try:
                streamed = await self._stream_reply(prompt, speak)
                if streamed is not None:
                    return streamed
            except Exception:  # any streaming hiccup → safe fallback (never worse)
                logger.exception("streaming reply failed; falling back to non-streamed")

        result = await self.generate(prompt, dispatcher, context)
        await speak(result.voice_text or result.final_text)
        return result

    async def _stream_reply(
        self, prompt: AssembledPrompt, speak: "Callable[[str], Awaitable[None]]"
    ) -> GenerationResult | None:
        """Stream the spoken reply as PLAIN prose (no JSON) so the first sentence
        starts synthesizing from the first tokens (§8.12). Speaks completed
        sentences as they arrive. Returns None (→ caller falls back) on an empty
        stream. Used only for plain conversational turns (no tool/live-info)."""
        instructions = _SPOKEN_REPLY_INSTRUCTIONS
        if prompt.emotion:
            instructions += f"\nDetected voice emotion signal: {json.dumps(prompt.emotion)}"
        messages = [
            *prompt.messages[:-1],
            {"role": "system", "content": instructions},
            prompt.messages[-1],
        ]

        text = ""
        spoken = 0
        async for delta in self._llm.stream(
            prompt.user_id,
            messages,
            prompt.complexity_hint,
            session_id=prompt.session_id,
            model=prompt.model_override,
        ):
            text += delta
            while (b := _sentence_end(text, spoken)) is not None:
                await self._speak_clean(text[spoken:b], speak)
                spoken = b

        if not text.strip():
            return None  # empty stream → fall back to the full path
        if spoken < len(text):  # flush the final (unterminated) sentence
            await self._speak_clean(text[spoken:], speak)

        # A streamed plain reply is a confident direct response by construction.
        judgment = Judgment(intent_confidence=0.9, ambiguity=0.1)
        return await self._finish_spoken(prompt, text, judgment)

    async def _speak_clean(self, sentence: str, speak: "Callable[[str], Awaitable[None]]") -> None:
        """Sanitize a sentence (keep whitelisted voice tags, drop assistant-speak)
        and hand it to TTS. Skips empties."""
        text = scrub_forbidden(_sanitize_tags(sentence)) or _sanitize_tags(sentence)
        if text.strip():
            await speak(text)

    async def _finish_spoken(
        self, prompt: AssembledPrompt, decoded_draft: str, judgment: Judgment
    ) -> GenerationResult:
        """Build the result for an already-spoken streamed reply (trace/memory)."""
        self._span(
            "judgment",
            intent=judgment.intent_confidence,
            novelty=judgment.novelty_score,
            salience=judgment.emotional_salience,
            ambiguity=judgment.ambiguity,
            complexity=judgment.complexity_tier,
            boundary_flag=judgment.capability_boundary_flag,
            streamed=True,
        )
        voice_text = _sanitize_tags(decoded_draft)
        return await self._finish(prompt, voice_text, "respond", judgment)

    async def _resolve_confirmation(
        self, prompt: AssembledPrompt, dispatcher: "ToolDispatch", context: "ToolContext"
    ) -> str | None:
        """The user is answering a pending action-tool confirmation (§8.3).

        Returns a tool-note that seeds generation so the LLM phrases the outcome
        in its own voice — never a static reply.
        """
        confirm = self._pending.pop(prompt.session_id)
        answer = prompt.utterance.strip().lower()
        if any(w in answer for w in _AFFIRMATIVE):
            try:
                result = await dispatcher.dispatch(
                    ToolCall(tool_id=confirm.tool_id, args=confirm.args), context, confirmed=True
                )
            except Exception as exc:  # never fake a write that failed
                logger.warning("confirmed tool %s failed: %s", confirm.tool_id, exc)
                return (
                    f"(the user confirmed '{confirm.tool_id}' but it FAILED to run — tell "
                    "them honestly it didn't go through; do not pretend it worked)"
                )
            output = getattr(result, "output", {})
            return (
                f"(the user confirmed the action '{confirm.tool_id}'; it ran and returned "
                f"{json.dumps(output)[:400]} — tell them it's done, briefly and naturally)"
            )
        if any(w in answer for w in _NEGATIVE):
            return (
                f"(the user declined the pending action '{confirm.tool_id}' — acknowledge "
                "warmly and move on, no big deal)"
            )
        return None  # neither yes nor no → they moved on; handle as a normal turn

    async def _finalize(self, prompt: AssembledPrompt, turn: LLMTurn) -> GenerationResult:
        """Apply the behavior gates to a final draft, then log + return (§9/§12)."""
        # Judgment span (trace §1.10): the model's own read of the turn.
        self._span(
            "judgment",
            intent=turn.judgment.intent_confidence,
            novelty=turn.judgment.novelty_score,
            salience=turn.judgment.emotional_salience,
            ambiguity=turn.judgment.ambiguity,
            complexity=turn.judgment.complexity_tier,
            boundary_flag=turn.judgment.capability_boundary_flag,
        )
        action = await self._curiosity_gate(prompt, turn.judgment)
        text = turn.draft_response

        boundary = await self._self_model.check_boundary(
            prompt.user_id, text, judgment_flag=turn.judgment.capability_boundary_flag
        )
        if boundary.flagged and boundary.rewritten_text:
            text = boundary.rewritten_text

        # Pull-based disclosure (rule 4) is generated by the model IN its draft
        # when the question requires it (requires_nature_disclosure) — one short,
        # natural, in-voice sentence, never a canned/appended disclaimer (§1.2).
        # No static string is bolted on here.

        text = _sanitize_tags(text)  # strip stray/echoed bracket tokens before TTS (V-TAGS-1)
        # When the turn genuinely requires a nature disclosure, the warm one-line
        # "I'm an AI, so I don't feel it the way you do" is DESIRED — don't let the
        # anti-disclaimer patterns scrub it (§1.2 rule 4). It's also emotionally
        # high-stakes and weak models answer it coldly, so warm-polish it on a
        # stronger tier before anything else.
        allow_disc = turn.judgment.requires_nature_disclosure
        if allow_disc:
            text = await self._warm_disclosure(prompt, text)
        # Self-reflection (§9.3): critique the draft against the response standard.
        # If it slipped into assistant-speak, re-say it in-voice once. The span is
        # emitted EVERY turn (trace §3.8) — ran/what-it-checked/whether-it-revised —
        # so the trace shows self-reflection actually happened, not only on a catch.
        draft_before = text  # F7: keep the pre-reflection draft for the trace
        flags_before = find_forbidden(text, allow_disclosure=allow_disc)
        revised = False
        scrubbed_used = False
        if self._self_reflect and flags_before:
            text = await self._rewrite_assistant_speak(prompt, text)
            revised = True
            # Deterministic safety net: if the rewrite still carries a banned
            # shape, drop the offending sentence(s) — but only if something
            # natural remains (never ship an empty reply).
            if find_forbidden(text, allow_disclosure=allow_disc):
                scrubbed = scrub_forbidden(text, allow_disclosure=allow_disc)
                if scrubbed:
                    text = scrubbed
                    scrubbed_used = True
        if self._self_reflect:
            self._span(
                "reflection",
                ran=True,
                checked="forbidden-assistant-speak",
                triggered_by=flags_before,
                revised=revised,
                scrubbed=scrubbed_used,
                clean_after=not find_forbidden(text, allow_disclosure=allow_disc),
                # F7: the actual draft → revision content, so the self-reflection
                # step is fully inspectable (not just whether it ran/revised).
                draft=draft_before,
                critique=(
                    f"forbidden assistant-speak: {flags_before}"
                    if flags_before
                    else "passed the response standard; no revision needed"
                ),
                revised_text=text if revised else "",
            )
        return await self._finish(prompt, text, action, turn.judgment)

    def _span(self, event: str, *, level: str = "info", **fields: Any) -> None:
        """Emit a reasoning span into the current turn's trace (via the logger)."""
        if self._logs is not None:
            self._logs.log(level, event, stage=event, **fields)

    async def _warm_disclosure(self, prompt: AssembledPrompt, text: str) -> str:
        """Warm-polish a nature-disclosure draft on a stronger tier (§1.2 rule 4).
        Keeps the original if the rewrite is empty or the provider is down."""
        instr = _DISCLOSURE_REWRITE_INSTRUCTIONS.format(q=prompt.utterance, draft=text)
        try:
            completion = await self._llm.complete(
                prompt.user_id,
                [{"role": "user", "content": instr}],
                _ESCALATE_TIER[self._reasoning_tier],  # stronger than the routed tier
                session_id=prompt.session_id,
                max_tokens=200,
            )
        except LLMUnavailable:
            return text
        candidate = _sanitize_tags(completion.text.strip())
        # Only accept the polish if it STILL discloses honestly — never let the
        # warm rewrite silently drop the required "I'm an AI" (§1.2 rule 4).
        if candidate and _HAS_DISCLOSURE.search(candidate):
            return candidate
        return text

    async def _rewrite_assistant_speak(self, prompt: AssembledPrompt, text: str) -> str:
        """One bounded rewrite pass to strip service-desk phrasing; keep the
        original if the rewrite is empty, worse, or the provider is down."""
        try:
            completion = await self._llm.complete(
                prompt.user_id,
                [{"role": "user", "content": _REWRITE_INSTRUCTIONS + text}],
                "simple",
                session_id=prompt.session_id,
            )
        except LLMUnavailable:
            return text
        candidate = _sanitize_tags(_strip_fences(completion.text)).strip().strip('"')
        # Only accept a rewrite that is non-empty and strictly cleaner.
        if candidate and len(find_forbidden(candidate)) < len(find_forbidden(text)):
            return candidate
        return text

    async def _dispatch_tool(
        self, dispatcher: "ToolDispatch", context: "ToolContext", req: ToolRequest
    ) -> "str | ConfirmRequest":
        """Run one tool per §8; return a note for the model, honest about failures.

        Live-info background tools (web_search) are resolved IN-TURN when they're
        quick (brief §8.8/§8.11) so the model answers now with real data instead of
        promising a result that only arrives later; a genuinely slow search falls
        back to the background/waiter path (§14).
        """
        call = ToolCall(tool_id=req.tool_id, args=req.args)
        spec = next((t for t in dispatcher.tools_for(context) if t.id == req.tool_id), None)
        is_background = spec is not None and (
            spec.type == "background" or spec.latency_class == "slow"
        )
        try:
            if is_background:
                outcome = await dispatcher.run_inline(call, context)
                # Item 5: run_inline returns a clean envelope, not a raise. Too slow
                # inline → promote to the queue (the background/waiter path).
                if isinstance(outcome, ToolResult) and outcome.status == "timeout":
                    outcome = await dispatcher.dispatch(call, context)
            else:
                outcome = await dispatcher.dispatch(call, context)
        except UnknownTool:
            return f"(tool '{req.tool_id}' is not available — do NOT claim you used it)"
        except Exception as exc:  # any residual raise — surface honestly, never fake it
            logger.warning("tool %s failed: %s", req.tool_id, exc)
            return (
                f"(tool '{req.tool_id}' isn't working right now — tell the user plainly "
                "that this step failed; do not fabricate a result)"
            )
        if isinstance(outcome, ConfirmRequest):
            return outcome
        if isinstance(outcome, QueuedHandle):
            return (
                f"(started '{req.tool_id}' in the background; its result will arrive at a "
                "pause — briefly say you're on it, then continue naturally)"
            )
        # A failed/timed-out tool envelope: tell the model plainly, don't fabricate.
        if isinstance(outcome, ToolResult) and not outcome.ok:
            return (
                f"(tool '{req.tool_id}' didn't complete ({outcome.status}) — tell the user "
                "plainly that this step failed; do not fabricate a result)"
            )
        return f"(tool '{req.tool_id}' returned: {json.dumps(outcome.output)[:1500]})"

    # ── steps ────────────────────────────────────────────────────────────

    async def _call_llm(
        self,
        prompt: AssembledPrompt,
        dispatcher: "ToolDispatch | None" = None,
        context: "ToolContext | None" = None,
        tool_notes: list[str] | None = None,
        budget: "_CostBudget | None" = None,
    ) -> LLMTurn | None:
        instructions = _JUDGMENT_INSTRUCTIONS
        if prompt.emotion:
            instructions += f"\nDetected voice emotion signal: {json.dumps(prompt.emotion)}"
        if dispatcher is not None and context is not None:
            instructions += _render_tool_instructions(
                dispatcher.tools_for(context), tool_notes or []
            )
        messages = [
            *prompt.messages[:-1],
            {"role": "system", "content": instructions},
            prompt.messages[-1],
        ]
        # Attempt 0: the MATURE reasoning tier (A2) + the user's explicit fast-model
        # choice if they opted into one. Attempt 1 (only on failure): escalate + drop
        # the pinned model. The main turn defaults to the mature model, not the
        # flashy fast tier — quality of thought over speed.
        base = self._reasoning_tier
        attempts = [(base, prompt.model_override), (_ESCALATE_TIER[base], None)]
        for attempt, (tier, model) in enumerate(attempts):  # rule 1: validate; retry once
            try:
                result = await self._llm.complete(
                    prompt.user_id,
                    messages,
                    tier,
                    response_format={"type": "json_object"},
                    session_id=prompt.session_id,
                    model=model,  # §4 user fast-model choice (first attempt only)
                )
            except LLMUnavailable:
                logger.warning("generation call failed (attempt %d)", attempt + 1)
                continue
            if budget is not None:
                budget.add(result.cost_usd)  # §10 cost ceiling accounting
            try:
                return LLMTurn.model_validate(json.loads(_strip_fences(result.text)))
            except (json.JSONDecodeError, ValidationError):
                logger.warning(
                    "judgment block failed validation (attempt %d): %.300s",
                    attempt + 1,
                    result.text,
                )
        return None

    async def _plain_reply(self, prompt: AssembledPrompt) -> str:
        """Robust no-JSON companion reply, used when the structured judgment path
        fails validation twice. Plain prose is far more reliable across models than
        the dual judgment+draft JSON, so a real turn (celebrating news, comforting)
        is salvaged instead of a canned fallback. Keeps the full persona; escalates
        a tier for reliability. Returns "" if the provider is fully down."""
        messages = [
            *prompt.messages[:-1],
            {"role": "system", "content": _SPOKEN_REPLY_INSTRUCTIONS},
            prompt.messages[-1],
        ]
        try:
            result = await self._llm.complete(
                prompt.user_id,
                messages,
                _ESCALATE_TIER[self._reasoning_tier],
                session_id=prompt.session_id,
                max_tokens=400,
            )
        except LLMUnavailable:
            logger.warning("plain-reply fallback failed; using canned safe line")
            return ""
        return result.text.strip()

    async def _curiosity_gate(self, prompt: AssembledPrompt, judgment: Judgment) -> Action:
        params = dict(DEFAULT_GATE_PARAMS)
        try:
            traits = await self._registry.enabled_traits(prompt.user_id)
        except ProfileNotFound:
            traits = []  # no profile yet → no per-user gating
        for trait in traits:
            if trait.id == "curiosity_policy":
                params.update({k: float(v) for k, v in trait.params.items()})
                break
        else:
            return "respond"  # trait disabled → gate off, always direct

        if judgment.intent_confidence < params["T_intent"]:
            return "clarify"
        if (
            judgment.ambiguity > params["T_ambig"]
            and judgment.emotional_salience > params["T_emotion"]
        ):
            # High ambiguity only matters when the stakes (salience) are high.
            return "clarify"
        # §3.1: suppress the novelty-driven follow-up in early sessions — with no
        # history everything looks "novel", so the gate would misfire constantly.
        if (
            not prompt.cold_start
            and judgment.novelty_score > params["T_novel"]
            and judgment.emotional_salience > params["T_emotion"]
        ):
            return "curious_followup"
        return "respond"

    async def _disambiguate(self, request: DisambiguationRequest) -> GenerationResult:
        names = [c.name for c in request.candidates[:3]]
        options = " or ".join(f'"{n}"' for n in names)
        text = f"Quick check — do you mean {options}?"
        result = GenerationResult(final_text=text, action="disambiguate", judgment=None)
        record = TurnRecord(user_id=request.user_id, confidence=0.3)
        await self._self_model.log(record, statement_text=text)
        result.turn_id = record.turn_id
        return result

    async def _finish(
        self,
        prompt: AssembledPrompt,
        text: str,
        action: Action,
        judgment: Judgment | None,
    ) -> GenerationResult:
        # ``text`` still carries whitelisted delivery tags (for TTS); the chat UI
        # and stored memory get the tag-free version (brief §1.4).
        voice_text = text
        clean_text = _strip_all_tags(text)
        # Rule 6: every turn logs to the self-model (cost is logged by §11).
        record = TurnRecord(
            user_id=prompt.user_id,
            confidence=judgment.intent_confidence if judgment else 0.2,
            facts_used=[c.entity_id for c in prompt.resolved_entities],
            novel_claim=bool(judgment and judgment.novelty_score > 0.7),
            capability_boundary_flag=judgment.capability_boundary_flag if judgment else None,
        )
        await self._self_model.log(record, statement_text=clean_text)
        # A required nature disclosure is not a style violation (§1.2 rule 4).
        allow_disc = bool(judgment and judgment.requires_nature_disclosure)
        style_flags = find_forbidden(clean_text, allow_disclosure=allow_disc)
        if style_flags:
            logger.warning("response contains forbidden assistant-speak: %s", style_flags)
        return GenerationResult(
            final_text=clean_text,
            voice_text=voice_text,
            action=action,
            judgment=judgment,
            turn_id=record.turn_id,
            style_flags=style_flags,
        )


# Whitelisted inline delivery tags (§23) — anything else in [...]/<...> is a
# stray/echoed token (e.g. the literal "[tags]" from the instructions) and is
# removed before the reply is shown or spoken (V-TAGS-1).
_ALLOWED_TAGS = frozenset(
    {
        "laugh",
        "laughs",
        "sigh",
        "sighs",
        "whisper",
        "whispers",
        "pause",
        "long pause",
        "short pause",
        "slow",
        "fast",
        "emphasis",
        "emphasize",
        "soft",
        "softly",
        "warm",
        "warmly",
        "gentle",
        "gently",
        "breath",
        "breathe",
        "gasp",
        "chuckle",
        "exhale",
        "sniff",
        "beat",
        "clears throat",
    }
)
_BRACKET_TOKEN = re.compile(r"\[([^\[\]]{1,24})\]|<([^<>]{1,24})>")


def _sanitize_tags(text: str) -> str:
    """Drop bracket/angle tokens whose inner word is not a known delivery tag.

    Keeps whitelisted delivery tags so the VOICE can perform them — this is the
    text handed to TTS and shown raw in the trace.
    """

    def keep(match: re.Match[str]) -> str:
        inner = (match.group(1) or match.group(2) or "").strip().lower()
        # Keep closing tags too (</emphasis>, </slow>) so a paired tag survives
        # intact to the voice — strip the leading slash before the whitelist check.
        base = inner[1:] if inner.startswith("/") else inner
        return match.group(0) if base in _ALLOWED_TAGS else ""

    cleaned = _BRACKET_TOKEN.sub(keep, text)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


# Sentence boundary for streaming spoken output (§8.12): punctuation + whitespace,
# so we never speak a half-formed sentence.
_SENTENCE_END_RE = re.compile(r"[.!?…][\"'\)\]]?\s")


def _sentence_end(text: str, start: int) -> int | None:
    """End index of the next complete sentence after ``start`` (punctuation +
    whitespace), or None — so we never speak a half-formed sentence."""
    match = _SENTENCE_END_RE.search(text, start)
    return match.end() if match else None


def _strip_all_tags(text: str) -> str:
    """Remove EVERY inline delivery token — whitelisted or not — for the
    user-facing chat text (brief §1.4). Tags shape the voice only; they must
    never render as literal text like "[gentle]" or "<pause>". The tagged form
    is preserved separately (``voice_text``) for TTS and the trace.
    """
    cleaned = _BRACKET_TOKEN.sub("", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


# Capability-refusal detector (brief §8.8): the whole class of bug where a weak
# fast model answers a live-info / unknown-term question with "I can't access
# real-time data" / "never heard of that" instead of using web_search. When the
# draft matches this AND the model ran no tool, the backstop forces a real search.
_CAPABILITY_REFUSAL = re.compile(
    r"don'?t have (access|the ability|live|real[- ]?time)"
    r"|can'?t (access|look up|check the|browse|get) "
    r"|no access to|not able to (access|look|check)"
    r"|real[- ]?time (data|info|information)"
    r"|drawing a blank"
    r"|outside (my|of my) knowledge|my knowledge (base|cut)"
    r"|(never|not) (heard|sure i'?ve heard) of"
    r"|don'?t (recognize|know (of|what|who))"
    r"|i'?m (just )?an ai",
    re.IGNORECASE,
)
# Hollow-promise detector: the model SAYS it will look something up but never
# dispatched a tool (observed on "top 2 news" → "just a moment while I get those"
# → then empty items). Treated the same as a refusal: run the search for real.
_HOLLOW_PROMISE = re.compile(
    r"\bjust a (moment|sec|second)\b"
    r"|\blet me (check|look|find|get|see|pull)\b"
    r"|\bi'?ll (check|look|find|get|pull)\b"
    r"|\bgive me a (moment|sec|second)\b"
    r"|\bhang on\b|\bone (moment|sec)\b|\bwhile i (get|find|look)\b",
    re.IGNORECASE,
)


def _needs_capability_repair(draft: str) -> bool:
    return bool(_CAPABILITY_REFUSAL.search(draft) or _HOLLOW_PROMISE.search(draft))


# Live-info INTENT in the user's own words (brief §8.8): these queries are about
# the current world and must be grounded in a real web_search, not the model's
# stale/guessed knowledge — regardless of how the draft is phrased. Kept to
# concrete topic markers (weather/news/time/price/score…) so ordinary emotional
# statements ("I'm stressed right now") never trip a search.
_LIVE_INFO_QUERY = re.compile(
    r"\b(weather|temperature|forecast"
    r"|news|headlines?|"
    r"scores?|who won"
    r"|stock price|share price|exchange rate"
    r"|current time|what(?:'s| is)? the time|time (?:in|right now)"
    r"|today'?s date|what(?:'s| is)? the date|date (?:today|in)"
    r"|what'?s happening (?:in|with|right now|today)|trending)\b",
    re.IGNORECASE,
)


def _is_live_info_query(utterance: str) -> bool:
    return bool(_LIVE_INFO_QUERY.search(utterance))


_REPAIR_INSTRUCTIONS = (
    "You just searched the live web for the user's question. Using ONLY these "
    "fresh search results, answer them directly, warmly, and briefly in your own "
    "voice (one or two spoken sentences) — give the ACTUAL answer, never say you "
    "can't find it or can't access it. If they asked for a set number of items "
    "(e.g. 'top 2 news'), give exactly that many, each a distinct item. "
    "Search results:\n"
)


def _render_tool_instructions(tools: list[ToolSpec], notes: list[str]) -> str:
    """List the available tools + any results so far for the agentic loop (§8)."""
    if not tools:
        return ""
    lines = [
        "\n\nYou can use tools to actually DO things (not just talk about them). "
        "For current/live info (news, scores, prices) use web_search; for the "
        "user's own saved data use the memory/project tools. Request at most one "
        "tool at a time via tool_request; when you have what you need, answer with "
        "tool_request null. Never claim you did something a tool didn't return.",
        "Available tools:",
    ]
    for tool in tools:
        lines.append(f"- {tool.id} ({tool.type}): {tool.description}")
    if notes:
        lines.append("Tool activity so far (use it; don't invent beyond it):")
        lines.extend(notes)
    return "\n".join(lines)


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    return stripped
