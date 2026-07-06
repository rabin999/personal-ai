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
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, BeforeValidator, ValidationError

from core.profile import ProfileNotFound, TraitRegistry
from core.reasoning.prompt_assembly import AssembledPrompt, DisambiguationRequest
from core.reasoning.self_model import BoundaryFlag, SelfModel, TurnRecord
from core.tools.dispatcher import ConfirmRequest, QueuedHandle, ToolCall
from core.tools.registry import ToolContext, ToolSpec, UnknownTool
from ports.llm import LLM, LLMUnavailable, Tier

logger = logging.getLogger(__name__)

Action = Literal["respond", "clarify", "curious_followup", "disambiguate"]

# Agentic tool loop (§8/§14.11): max tool round-trips before the model must answer.
MAX_TOOL_STEPS = 4


class ToolDispatch(Protocol):
    """The slice of the §13 dispatcher the response loop needs."""

    def tools_for(self, context: ToolContext) -> list[ToolSpec]: ...

    async def dispatch(
        self, call: ToolCall, context: ToolContext, *, confirmed: bool = False
    ) -> Any: ...

# Default gate thresholds; overridden by curiosity_policy trait params.
DEFAULT_GATE_PARAMS = {"T_intent": 0.55, "T_novel": 0.7, "T_emotion": 0.6, "T_ambig": 0.65}

# Regex BACKSTOP for pull-based disclosure (rule 4). The primary trigger is the
# model's `requires_nature_disclosure` judgment (V-DISCLOSE-1); these patterns
# only catch obvious cases if the judgment misses. + the one folded-in sentence.
_DISCLOSURE_PATTERNS = (
    r"\bdo you (?:actually|really|truly) (?:care|love|like)\b",
    r"\bare you (?:real|human|alive|conscious|sentient)\b",
    r"\bdo you (?:have|feel) (?:feelings|emotions)\b",
    r"\bdo you love me\b",
    r"\bwhat are you really\b",
)
_DISCLOSURE_SENTENCE = (
    "I should be straight with you: I'm an AI — I don't feel things the way "
    "you do, but I do genuinely track what matters to you."
)

_SAFE_FALLBACK_TEXT = (
    "I want to make sure I get this right — tell me a bit more about what "
    "you mean?"
)

# Graceful ack when a background lookup is running but the final compose failed.
_LOOKING_UP_TEXT = "On it — looking that up now, I'll tell you in a sec."

_JUDGMENT_INSTRUCTIONS = """
Respond ONLY with a JSON object of this exact shape:
{"draft_response": "<your reply; short, warm, natural spoken language; may
   contain TTS tags like [sigh] or <pause>>",
 "judgment": {"intent_confidence": <0..1 how sure you are of what the user wants>,
              "novelty_score": <0..1 how new this topic is for this user>,
              "emotional_salience": <0..1 emotional weight of this moment>,
              "ambiguity": <0..1 how ambiguous the request is>,
              "complexity_tier": "simple|moderate|complex",
              "requires_nature_disclosure": <true ONLY if the user is asking about
                 YOUR nature in a way that needs an honest "I'm an AI" to answer
                 truthfully — e.g. "do you actually care?", "are you human/real/
                 a bot?", "would you miss me?", "do you have feelings?". false for
                 everything else — never volunteer it>,
              "capability_boundary_flag": null | "overclaim_empathy" | "overclaim_consciousness"},
 "tool_request": null | {"tool_id": "<id>", "args": {<per the tool's schema>}}}
Set capability_boundary_flag if your draft claims felt emotion or consciousness.
tool_request: leave null unless you need a tool to answer well (see tools below).
Ground every factual claim about the user in the conversation and the provided
memories/facts. If the answer is not in your context, say you don't remember —
NEVER invent details about the user's life.
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
    final_text: str
    action: Action
    judgment: Judgment | None = None
    turn_id: str | None = None


class ResponseGenerator:
    def __init__(self, llm: LLM, self_model: SelfModel, registry: TraitRegistry) -> None:
        self._llm = llm
        self._self_model = self_model
        self._registry = registry

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

        tool_notes: list[str] = []
        enqueued_background = False
        can_use_tools = dispatcher is not None and context is not None
        turn: LLMTurn | None = None
        for _ in range(MAX_TOOL_STEPS if can_use_tools else 1):
            turn = await self._call_llm(prompt, dispatcher, context, tool_notes)
            if turn is None:  # both attempts failed validation / provider down
                # If a lookup is already running, acknowledge it gracefully rather
                # than the generic clarify fallback (the result arrives at a pause).
                if enqueued_background:
                    return await self._finish(prompt, _LOOKING_UP_TEXT, "respond", None)
                return await self._finish(prompt, _SAFE_FALLBACK_TEXT, "clarify", judgment=None)
            if turn.tool_request is None or not can_use_tools:
                break
            note = await self._dispatch_tool(dispatcher, context, turn.tool_request)  # type: ignore[arg-type]
            if isinstance(note, ConfirmRequest):  # §8.3: action needs confirmation
                return await self._finish(prompt, note.prompt_text, "respond", turn.judgment)
            if "in the background" in note:
                enqueued_background = True
            tool_notes.append(note)
        assert turn is not None
        return await self._finalize(prompt, turn)

    async def _finalize(self, prompt: AssembledPrompt, turn: LLMTurn) -> GenerationResult:
        """Apply the behavior gates to a final draft, then log + return (§9/§12)."""
        action = await self._curiosity_gate(prompt, turn.judgment)
        text = turn.draft_response

        boundary = await self._self_model.check_boundary(
            prompt.user_id, text, judgment_flag=turn.judgment.capability_boundary_flag
        )
        if boundary.flagged and boundary.rewritten_text:
            text = boundary.rewritten_text

        # Pull-based disclosure (rule 4): fire on the model's intent judgment —
        # whether the question needs an honest "I'm an AI" to answer — with a
        # small regex only as a backstop (V-DISCLOSE-1). Never volunteered.
        needs_disclosure = (
            turn.judgment.requires_nature_disclosure or _wants_disclosure(prompt.utterance)
        )
        if needs_disclosure and not _already_discloses(text):
            text = f"{text} {_DISCLOSURE_SENTENCE}"

        text = _sanitize_tags(text)  # strip stray/echoed bracket tokens before TTS (V-TAGS-1)
        return await self._finish(prompt, text, action, turn.judgment)

    async def _dispatch_tool(
        self, dispatcher: "ToolDispatch", context: "ToolContext", req: ToolRequest
    ) -> "str | ConfirmRequest":
        """Run one tool per §8; return a note for the model, honest about failures."""
        call = ToolCall(tool_id=req.tool_id, args=req.args)
        try:
            outcome = await dispatcher.dispatch(call, context)
        except UnknownTool:
            return f"(tool '{req.tool_id}' is not available — do NOT claim you used it)"
        except Exception as exc:  # surface failures honestly (never pretend it worked)
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
        return f"(tool '{req.tool_id}' returned: {json.dumps(outcome.output)[:1500]})"

    # ── steps ────────────────────────────────────────────────────────────

    async def _call_llm(
        self,
        prompt: AssembledPrompt,
        dispatcher: "ToolDispatch | None" = None,
        context: "ToolContext | None" = None,
        tool_notes: list[str] | None = None,
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
        for attempt in range(2):  # rule 1: validate; retry once
            try:
                result = await self._llm.complete(
                    prompt.user_id,
                    messages,
                    prompt.complexity_hint,
                    response_format={"type": "json_object"},
                    session_id=prompt.session_id,
                )
            except LLMUnavailable:
                logger.warning("generation call failed (attempt %d)", attempt + 1)
                continue
            try:
                return LLMTurn.model_validate(json.loads(_strip_fences(result.text)))
            except (json.JSONDecodeError, ValidationError):
                logger.warning(
                    "judgment block failed validation (attempt %d): %.300s",
                    attempt + 1,
                    result.text,
                )
        return None

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
        if (
            judgment.novelty_score > params["T_novel"]
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
        # Rule 6: every turn logs to the self-model (cost is logged by §11).
        record = TurnRecord(
            user_id=prompt.user_id,
            confidence=judgment.intent_confidence if judgment else 0.2,
            facts_used=[c.entity_id for c in prompt.resolved_entities],
            novel_claim=bool(judgment and judgment.novelty_score > 0.7),
            capability_boundary_flag=judgment.capability_boundary_flag if judgment else None,
        )
        await self._self_model.log(record, statement_text=text)
        return GenerationResult(
            final_text=text, action=action, judgment=judgment, turn_id=record.turn_id
        )


# Whitelisted inline delivery tags (§23) — anything else in [...]/<...> is a
# stray/echoed token (e.g. the literal "[tags]" from the instructions) and is
# removed before the reply is shown or spoken (V-TAGS-1).
_ALLOWED_TAGS = frozenset({
    "laugh", "laughs", "sigh", "sighs", "whisper", "whispers", "pause",
    "long pause", "short pause", "slow", "fast", "emphasis", "emphasize",
    "soft", "softly", "warm", "warmly", "gentle", "gently", "breath", "breathe",
    "gasp", "chuckle", "exhale", "sniff", "beat", "clears throat",
})
_BRACKET_TOKEN = re.compile(r"\[([^\[\]]{1,24})\]|<([^<>]{1,24})>")


def _sanitize_tags(text: str) -> str:
    """Drop bracket/angle tokens whose inner word is not a known delivery tag."""

    def keep(match: re.Match[str]) -> str:
        inner = (match.group(1) or match.group(2) or "").strip().lower()
        return match.group(0) if inner in _ALLOWED_TAGS else ""

    cleaned = _BRACKET_TOKEN.sub(keep, text)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


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


def _wants_disclosure(utterance: str) -> bool:
    lowered = utterance.lower()
    return any(re.search(pattern, lowered) for pattern in _DISCLOSURE_PATTERNS)


def _already_discloses(text: str) -> bool:
    lowered = text.lower()
    return "i'm an ai" in lowered or "i am an ai" in lowered


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    return stripped
