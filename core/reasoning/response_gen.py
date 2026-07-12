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

import asyncio
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, BeforeValidator, ValidationError

from core.errors import PROGRAMMING_ERRORS
from core.observability.logger import StructuredLogger
from core.phrases import defaults as default_pools
from core.phrases.catalog import PhraseCatalog
from core.profile import ProfileNotFound, TraitRegistry
from core.reasoning.prompt_assembly import AssembledPrompt, DisambiguationRequest
from core.reasoning.prosody import (
    effective_register,
    prosody_directive,
    read_register,
    somber_content,
    strip_inappropriate_tags,
)
from core.reasoning.self_model import BoundaryFlag, SelfModel, TurnRecord
from core.reasoning.style import (
    excise_forbidden,
    find_forbidden,
    is_bare_acknowledgement,
    scrub_forbidden,
    strip_slang,
    strip_tool_leak,
)
from core.reasoning.voice_effects import (
    apply_effect_override,
    build_demo,
    detect_demo_request,
    detect_effect_override,
)
from core.reasoning.volatility import is_volatile_question
from core.tools.builtin.core_tools import _strip_unrequested_date
from core.tools.dispatcher import ConfirmRequest, QueuedHandle, ToolCall, ToolResult
from core.tools.registry import ToolContext, ToolSpec, UnknownTool
from ports.llm import LLM, LLMUnavailable, Tier
from ports.prompt import PromptProvider

logger = logging.getLogger(__name__)

Action = Literal["respond", "clarify", "curious_followup", "disambiguate"]

T = TypeVar("T")

# Agentic tool loop (§8/§14.11): max tool round-trips before the model must answer.
MAX_TOOL_STEPS = 4

# P2 temperature-per-step: the companion REPLY runs MODERATE for natural warmth and
# variation (a near-zero temp here is the stiff, robotic voice we fight). Decision/
# routing/extraction calls run LOW elsewhere for consistency. Both Gemini and Claude
# accept 0-1; 0.7 is comfortably mid-range.
REPLY_TEMPERATURE = 0.7
# P1 max_tokens-per-step: a GENEROUS safety ceiling (stops runaway generation) sized
# for a natural SPOKEN reply — never a brevity tool (brevity comes from the persona).
# Voice replies are short; 800 tokens is far above a normal reply so it never clips.
REPLY_MAX_TOKENS = 800


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
# TOTAL model outage (every provider in the chain failed — e.g. out of credits / all 402,
# a full API outage). The warm "I'm right here" line is a LIE here: nothing is thinking. Say
# so honestly instead (user report). Kept free of banned assistant-speak so enforcement
# never rewrites it, and not exposing "credits" — just that the system is temporarily down.
_SERVICE_DOWN_TEXT = (
    "Sorry — I can't think straight right now, looks like my system's temporarily down on my "
    "end. Give it a minute and try me again?"
)

# S1: the reasoning step judged this answer would go stale without a live lookup, and the
# lookup failed. Say so honestly rather than shipping a training-data answer as fact (§16).
_SEARCH_FAILED_TEXT = (
    "I tried to look that up just now and couldn't get through — "
    "I don't want to tell you something that's out of date. Want me to try again?"
)
# The search RAN but turned up nothing usable, and the model answered with a hollow promise
# ("I'll do my best to find that for you") instead of saying so. A promise the turn cannot
# keep is worse than an honest miss (§16).
_NOT_FOUND_TEXT = (
    "I had a look and couldn't find anything current on that — I'd rather tell you that than guess."
)

# Progressive retrieval (RETRIEVAL_POLICY.md): a complex question ("someone burned and
# died in Nepal over a fine") needs SEVERAL focused lookups — the specific incident, the
# cause, the related rule/number — not one shallow query. This plans the distinct facets to
# fetch; a simple question yields one query. The model decides how much is genuinely needed.
_MULTIQ_PLAN_INSTRUCTIONS = (
    "The user asked something that may take MORE THAN ONE web search to answer well.\n"
    "List the DISTINCT facts you would need to look up to answer it fully — each as a focused "
    "search query. HARD RULES:\n"
    "- Every query must be SELF-CONTAINED and name the SPECIFIC subject — the exact event/"
    "entity/place. A query like 'authorities' statement on the missing plane' is useless: WHICH "
    "plane? Carry the concrete identifiers (place, what happened) into EVERY query.\n"
    "- Do NOT append a specific date/month/year (e.g. 'July 2026'); use 'latest' or 'recent' — "
    "the results are ranked by recency.\n"
    "- Include ONLY what is genuinely needed: a simple factual question needs ONE query; a "
    "multi-part or news question may need 2-3 (the specific event; its cause; the official "
    "response). Do NOT emit near-duplicate phrasings of the same facet.\n"
    "Reply with ONE query per line, at most 3, most important first."
)
# After the first round of searches, decide whether an ESSENTIAL facet is still missing and,
# only if so, name up to two more focused queries. Bounded so retrieval never loops forever.
_MULTIQ_GAP_INSTRUCTIONS = (
    "You searched the web and gathered the findings below for the user's question. Decide if "
    "an ESSENTIAL fact needed to answer them is STILL missing.\n"
    "If the findings are enough to answer, reply with exactly: ENOUGH\n"
    "If something essential is missing, reply with up to TWO more focused, self-contained "
    "search queries (one per line) for JUST the missing facts — nothing already covered."
)

# Deterministic interjection pools (§8.12 progressive delivery). Curated, FACT-FREE lines —
# spoken to fill the beat before the real answer lands, so they can never contradict it (an
# LLM-written ack once hallucinated a temperature that clashed with the real answer). Picked at
# random (avoiding a back-to-back repeat) so it stays lively without a model call. The canonical
# text lives in `core.phrases.defaults`; a background regenerator refreshes the LIVE pools through
# the injected `PhraseCatalog` so they don't feel static — but these defaults are always the safe
# fallback. Names kept here for existing imports/tests.
_ACK_EMPATHY = default_pools.ACK_EMPATHY
_ACK_LOOKUP = default_pools.ACK_LOOKUP
_ACK_THINKING = default_pools.ACK_THINKING
# Recall turns ("what did we talk about last time") — the beat is reading back through stored
# conversations, so the filler nods to THAT, not a web lookup.
_ACK_RECALL = default_pools.ACK_RECALL
# PROGRESS fillers: the FIRST interjection ("let me check") already played; these fill the LATER
# beats while the search/generation is still running and the audio has gone quiet.
_ACK_PROGRESS_LOOKUP = default_pools.PROGRESS_LOOKUP
_ACK_PROGRESS_THINKING = default_pools.PROGRESS_THINKING
# When the wait really drags, soften to a gentle apology (later beats, not the first).
_ACK_PROGRESS_APOLOGY = default_pools.PROGRESS_APOLOGY

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
HOW YOU TALK — this matters as much as WHAT you say, and it is where you usually fail:
- Reply like a close friend talking, NOT an assistant. Usually ONE short sentence, two at MOST.
- Casual: contractions, plain everyday words. NEVER a paragraph. NEVER a news-anchor readout.
- When they share something HARD: meet it in a few genuine words, then ask ONE real, specific
  question — e.g. "I'm sorry, that's stressful. How's he doing now?" Do NOT monologue about how
  understandable their feelings are; NO greeting-card sympathy ("sending my best thoughts", "I'm
  here for you", "that must be so draining").
- When they share GOOD news: react with short, real excitement — a line, not a speech.
- Use their name ALMOST NEVER. If a third sentence is forming, cut it.
- BUT when they ask a REAL question that needs information — "what are the types of X",
  "how does Y work", "the difference between A and B", "explain Z" — the one-sentence cap
  does NOT apply. Give a CLEAR, COMPLETE, CORRECT answer with the actual substance and the
  real terms: name the actual kinds/steps/parts, say what each one is in a few words. Still
  casual and concise (no lecture, no textbook tone) — but NEVER vague hand-waving like "some
  people throw in graph stuff" or "stuff like that". Informal does NOT mean clueless: know
  what you're talking about and say it plainly. A friend who actually knows the topic
  explains it clearly, they don't mumble. This is SPOKEN ALOUD, so expand a technical
  acronym in words the first time you use it — "retrieval-augmented generation, RAG for
  short", not a bare "RAG" — so it's clear out loud; after that the short form is fine.

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
 "draft_response": "<your reply — ONE or two sentences, MAX. Informal and casual by
   default: contractions, plain everyday words, warm and natural like a thoughtful friend —
   but still POLISHED and respectful, the way you'd talk with students and professionals.
   NO frat-boy slang ('dude', 'bro', 'bruh', 'man') and no filler like 'kinda'/'sorta' in
   place of a real word — casual is not sloppy. NOT a news anchor, NOT a report. Only get
   measured or formal when the moment is genuinely serious, technical, or emotional; if a
   third sentence is forming, cut it.
   Use their NAME only rarely, the way real friends do — most replies use no name at
   all, never every turn. Natural spoken language. The voice
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
Reply out loud in your own natural voice, by your NAME (never call yourself 'an
AI'). KEEP IT SHORT — usually ONE sentence, at most two, like a friend actually
talking. Stay INFORMAL and casual by default — contractions, plain everyday words,
warm and natural like a thoughtful friend, but still POLISHED and respectful — the
way you'd talk with students and professionals, NOT frat-boy slang ('dude', 'bro',
'man', 'bruh') and not sloppy filler ('kinda', 'sorta') standing in for a real word;
only get more measured or formal when the moment is genuinely serious or emotional.
Don't explain or elaborate unless they ask; if a third sentence is forming, cut it.
BUT when they DO ask a real
question that needs information — types of something, how something works, the
difference between things, "explain X" — drop the one-sentence cap and actually answer
it: name the real kinds/steps/parts and say what each is, clearly and completely, still
casual and concise. NEVER vague hand-waving ("some people throw in graph stuff", "stuff
like that") — informal does NOT mean clueless; if you know the topic, say it plainly and
correctly. This is SPOKEN ALOUD, so expand a technical acronym in words the first time —
say "retrieval-augmented generation, RAG for short", not a bare "RAG"; "a large language
model, an LLM" — then the short form is fine. Do NOT reflexively end on a
stock filler question — 'what's on your mind?', 'what's up?', 'what's going on?',
'anything on your mind?' — you lean on these way too much. But DO keep the conversation
ALIVE: a real friend stays curious and follows the thread. React with something SPECIFIC to
what they actually said — pick up the interesting detail, offer a genuine thought or reaction,
and when it fits, ask ONE real question that moves the topic forward (never a generic prompt).
If THEY ask YOU something back ('how are you?', 'what about you?', 'you doing okay?'), always
answer it AND turn it back with real interest ('doing good, thanks — how's YOUR day treating
you?'), never a flat 'thanks for asking' that leaves them hanging. Almost never end a turn on a
dead stop — give them something to respond to; just make it specific, not a canned prompt.
Don't just acknowledge and stop dead, and don't interrogate — carry it like a real back-and-
forth. Be easy and grounded, not gushing or overly warm — match their energy,
save real tenderness for when they're actually going through something. Use their NAME
only sparingly — real friends don't say each other's name every sentence; most replies
use no name at all, never every turn. Work out what
they really mean and respond to THAT; never stall with 'what do you mean?'. Weave in 1-2
inline delivery tags only where they genuinely fit: [laugh] [chuckle] [sigh] for
feeling; [warm] [gentle] [soft] for tone; <emphasis>word</emphasis>; <pause> — never
tag every sentence, and never a laugh on a sad turn. If they DIRECTLY ask whether
you're real or an AI, acknowledge it in one short half-sentence and move on — never a
canned disclaimer, never lead with it. Never use assistant / service-desk phrasing.
Deliver facts the way a human SAYS them: local clock time (never a UTC offset), their
units/currency, search/tool data paraphrased into natural speech (never read tables/
codes/IDs), concrete answer first. Reply with ONLY the spoken words — no JSON, no
quotes, no preamble.
""".strip()


# Grok Voice performs two KINDS of inline effect; using both (sparingly, where they fit) is
# what makes a spoken reply sound alive instead of flat. Shared by every spoken path so the
# fillers' answer, the plain reply, and the searched answer all get the same natural delivery.
_DELIVERY_TAGS_GUIDE = (
    "PERFORM it with the voice's delivery effects so it doesn't sound flat — sparingly, only "
    "where they genuinely fit. There are two kinds:\n"
    "• INSTANT effects fire at a single point: [laugh] [chuckle] [sigh] [gasp] [breath] "
    "<pause> — one well-placed beat of real feeling.\n"
    "• WRAPPING effects shape a SPAN and must be OPENED and CLOSED around the exact words: "
    "<emphasis>really</emphasis>, <slow>take your time</slow>, <whisper>between us</whisper>.\n"
    "Tone markers set the register: [warm] [gentle] [soft]. On any real conversational turn "
    "weave in AT LEAST ONE delivery beat so the voice sounds alive, not flat — a [warm]/[gentle] "
    "tone marker, a <pause>, or an <emphasis> on the word that carries the feeling; two is plenty, "
    "never tag every sentence, and none is fine only on a terse one-word reply ('yeah', 'got it'). "
    "ALWAYS close a wrapping tag; never a laugh on a sad or grave turn."
)


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
    # What the style detector CAUGHT on the draft, before `_enforce` acted on it (§7).
    #
    # This used to hold the flags found on the FINAL text — and the engine returned that text
    # regardless, which is D-7. Now that enforcement guarantees the final text is clean, a
    # field holding "flags on the final text" is empty by construction, and the gate row
    # `flagged drafts that became the reply = 0` would read PASS for the same reason an
    # unplugged smoke alarm never sounds. The mutation `style_flags_never_reported` proved
    # exactly that: after the D-7 fix, setting this field to `[]` became an *equivalent*
    # mutation that no test could kill.
    #
    # So it now records what enforcement caught. It is observability, not a verdict. The
    # verdict is `find_forbidden(final_text)`, which every caller computes for itself and
    # which must always be empty.
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

# Labels that mean "the ONLY problem is a STOCK filler question" — the reply was trying to
# engage, it just reached for a canned phrase ('what's on your mind?'). Deleting that sentence
# (scrub_forbidden) leaves a dead-end reply that abandons the user (reported: "I'm great, how
# are you?" → "Doing well, thanks for asking!" and nothing more). For these we REPLACE the
# stock question with a genuine one instead of dropping it — see `_repair_flat_filler`.
_ENGAGEMENT_FILLER_LABELS = frozenset({"flat filler opener", "flat filler reply"})

_ENGAGEMENT_REWRITE_INSTRUCTIONS = (
    "This spoken reply is warm but it ends on a STOCK filler question ('what's on your mind?', "
    "'what's up?', 'what's going on?', 'anything on your mind?'). Keep the reply and its feeling, "
    "but REPLACE that stock question with a REAL, specific one that keeps the conversation going: "
    "reciprocate if they asked you something ('...how about you?', 'and how's YOUR day been?'), or "
    "pick up the concrete thread of what they said and ask about THAT. Do NOT just delete the "
    "question and stop flat — a friend always gives them something to respond to. One or two "
    "spoken sentences, keep any inline [tags]/<tags>. Reply with ONLY the rewritten line.\n\nLine: "
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

# Brevity/register ENFORCEMENT (#4). The reasoning model (gemini-flash) reliably ignores
# "keep it short" in the draft prompt and produces long, greeting-card-warm replies — proven
# repeatedly. So brevity is enforced here (mechanism, not advice, per the D-6/D-7 lesson): a
# reply that is too long OR reads like a greeting card on a normal conversational turn is
# compressed to how a real friend actually talks.
_GREETING_CARD = re.compile(
    r"sending (you )?(all )?my (best|good) (thoughts|wishes)"
    r"|i'?m here for you|here for you if you|i'?m here if you"
    r"|that must be (so|really|incredibly) (hard|tough|draining|difficult|stressful|exhausting)"
    r"|it'?s (completely |totally |so )?understandable that you"
    r"|please know that|whenever you (need|want) to (talk|chat|vent)"
    r"|my (heart goes out|thoughts are with)|i can only imagine how",
    re.IGNORECASE,
)


# Genuine informational/explanatory questions — "types of RAG", "how does X work",
# "difference between A and B", "explain …". These need a CLEAR, COMPLETE answer with
# real substance, so the brevity clamp (below) must NOT gut them into vague hand-waving
# ("some people throw in graph stuff"). Informal ≠ clueless. The clamp still applies to
# chit-chat and emotional turns; it just doesn't fire when the user actually asked to be
# informed. Deliberately narrow so it never catches "how are you" / "what's up".
_WANTS_SUBSTANCE = re.compile(
    r"\b(types?|kinds?|sorts?|categories|kind)\s+of\b"
    r"|\bdifferences?\s+between\b"
    r"|\bhow\s+(do(es)?|can|would|should)\b[^?]*\bwork\b"
    r"|\bhow\s+(to|do\s+i|do\s+you|can\s+i)\b"
    r"|\bexplain\b|\bwalk\s+me\s+through\b|\bbreak\s+(it|this|that|down)\b"
    r"|\bpros\s+and\s+cons\b|\btrade-?offs?\b|\badvantages?\b|\bdisadvantages?\b"
    r"|\bwhy\s+(do(es)?|is|are|would|can|should)\b"
    r"|\bwhat('?s|\s+is|\s+are)\s+(the\s+)?(main\s+|different\s+|various\s+)?(steps?|ways?|"
    r"methods?|approaches?|options?|components?|parts?)\b"
    r"|\bcompare\b|\blist\s+(the|some|of|out|a\s+few)\b|\bwhat\s+are\s+the\b",
    re.IGNORECASE,
)


def _wants_substance(question: str) -> bool:
    """True when the user asked a genuine informational/explanatory question that
    deserves a clear, complete answer — so the brevity clamp should not compress it."""
    return bool(_WANTS_SUBSTANCE.search(question))


def _recent_convo(prompt: "AssembledPrompt", n: int = 6) -> str:
    """The last few conversation turns (excluding the current question) as plain text, so
    search-query building and answer composition can resolve implicit references — e.g. the
    COUNTRY when the user has been discussing Nepal/NEPSE and then asks about 'the new
    government'. Reads the assembled message list, which already holds the recent turns."""
    prior = prompt.messages[:-1][-n:]  # drop the current utterance; keep the n before it
    lines = [
        f"{m.get('role', 'user')}: {m.get('content', '')}"
        for m in prior
        if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()
    ]
    return "\n".join(lines)


# Short social pleasantries / acks that get an INSTANT reply — a "let me think" interjection in
# front of these is annoying and wrong-toned. Mirrors the orchestrator's `_TRIVIAL_SOCIAL`; kept
# here so the core quick-ack gate needs no adapter import (ports boundary, §17.3).
_TRIVIAL_TURN = re.compile(
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


def _wants_quick_ack(prompt: "AssembledPrompt") -> bool:
    """True when a plain (streamable) turn is likely to make the user WAIT, so a quick fact-free
    interjection should fill the silence first.

    Why this fires broadly: the streamable path BUFFERS the whole reply (for self-reflection +
    the §12 gates, C1) before speaking a single word, so on any turn that isn't near-instant the
    user hears dead air until the full generation lands. A recall turn reads back stored
    conversations; a substantive/multi-part question takes a beat; and honestly so does any real
    conversational turn. So the gate is now "fire UNLESS it's a short throwaway pleasantry" — the
    prior recall/substance/multipart-only gate left most ordinary turns silent up front (reported:
    "quick sentences not often triggering"). A trivial/social line ("hi", "yeah", "how are you?",
    "thanks") still returns False: it gets an instant reply and a "let me think" there is annoying.
    """
    if prompt.recall_source in ("past", "current"):
        return True
    utt = (prompt.utterance or "").strip()
    if _is_trivial_turn(utt):
        return False
    if _wants_substance(utt) or _is_multipart_question(utt):
        return True
    # Any remaining non-trivial turn of real length takes a beat to answer well — fill it.
    return len(utt.split()) >= 5


def _is_trivial_turn(utterance: str) -> bool:
    """True for a whole-utterance greeting / social pleasantry / short ack that gets an instant
    reply — so the quick interjection is suppressed there (it would only stall a fast turn)."""
    return bool(_TRIVIAL_TURN.match((utterance or "").strip()))


def _reads_verbose(text: str, question: str = "") -> bool:
    """True when a conversational reply is longer or more greeting-card than a friend
    would say. An informational/explanatory question is exempt from the plain length
    cap (it needs room to answer fully) but never from the greeting-card check."""
    clean = _sanitize_tags(text)
    if bool(_GREETING_CARD.search(clean)):
        return True
    if _wants_substance(question):
        # Room to actually explain — still bounded so it can't become a lecture.
        sentences = len([s for s in re.split(r"[.!?]+", clean) if s.strip()])
        return sentences > 6 or len(clean.split()) > 120
    sentences = len([s for s in re.split(r"[.!?]+", clean) if s.strip()])
    words = len(clean.split())
    return sentences > 2 or words > 40


_BRIEF_REWRITE_INSTRUCTIONS = (
    "Rewrite this reply the way a close friend would actually say it OUT LOUD — short and human:\n"
    "- ONE or two short sentences. Casual: contractions, plain everyday words.\n"
    "- If they shared something HARD, meet it in a few genuine words, then ask ONE real, specific "
    "question. NO greeting-card lines ('sending my best thoughts', 'here for you', 'that must be "
    "so draining', 'it's understandable that you feel...', 'please know that').\n"
    "- If it's GOOD news, a short genuine reaction — not a speech.\n"
    "- Keep the actual meaning and any facts; add NOTHING new. Keep any inline [tags]/<tags>.\n"
    "- Use their name almost never. Reply with ONLY the rewritten spoken words, no quotes.\n\n"
    "Reply to rewrite:\n"
)

# A valid disclosure still names the AI nature honestly — guards the warm rewrite
# from silently dropping it.
_HAS_DISCLOSURE = re.compile(
    r"\ban ai\b|\bi'?m an ai\b|\bi am an ai\b|\bnot (a )?(real )?(human|person)\b|\ba bot\b",
    re.IGNORECASE,
)

# What each detector label means, in plain language the rewrite model can act on (C2).
# A generic "remove assistant phrasing" instruction left the offending shape in place.
_FLAG_GUIDANCE: dict[str, str] = {
    "corporate apology": "the customer-service apology ('I'm sorry for/about/if…', "
    "'I'm doing my best', 'bear with me') — a friend doesn't apologise like a call centre",
    "service framing": "the service framing ('get you the information', 'gather all the "
    "details', 'I don't have enough information') — just talk like a person",
    "service-desk opener": "the service-desk question ('what can I help you with')",
    "clarifier": "the clarifying question ('do you mean…?', 'or something else?') — "
    "engage with what they obviously meant instead of interrogating them",
    "clarifier hedge": "the QA hedge ('I want to make sure I get this right')",
    "cold feeling denial": "the cold denial ('I don't feel emotions', 'I don't have "
    "feelings') — if you must be honest that you're an AI, lead with the genuine "
    "attention you DO pay them and keep it to one warm sentence",
    "volunteered AI disclaimer": "the volunteered AI disclaimer — they didn't ask",
    "availability advert": "advertising your availability ('I'm always here for you')",
    "assistant offer": "the assistant offer ('happy to help', 'I can help with that')",
    "assistant-speak": "the assistant phrasing ('feel free to ask', 'is there anything else')",
    "flat filler opener": "the stock filler question ('what's on your mind')",
    "flat filler reply": "a reply that is nothing but a stock filler question — say something real",
    "self-announcement": "announcing your own name at the top like a receptionist",
    "nature monologue": "the defensive monologue about not being a person",
    "assistant-existence framing": "describing your existence as processing/assisting",
    "bolted-on disclaimer": "the bolted-on 'not a substitute for real people' disclaimer",
    "ToS disclaimer": "the ToS-style disclaimer",
}


def _critique_note(flags: list[str]) -> str:
    """Name the exact shapes the detector found, so the rewrite can remove THEM."""
    seen: list[str] = []
    for f in flags:
        note = _FLAG_GUIDANCE.get(f)
        if note and note not in seen:
            seen.append(note)
    if not seen:
        return ""
    bullets = "\n".join(f"- {s}" for s in seen)
    return (
        "\n\nYour line was flagged for the following. Remove EVERY one of them, keep the "
        f"genuine content, and stay in your own warm voice:\n{bullets}"
    )


# Does the user's message directly ask about the companion's NATURE (§1.2 rule 4)?
# The streaming voice path has no LLM judgment block to tell it, so it derives the
# disclosure flag here. High precision: a false positive costs one warm-polish call; a
# false negative ships the cold "I don't feel emotions like a person does" line.
_NATURE_QUESTION = re.compile(
    r"\bdo you (actually |really |even |truly )?(care|feel|love|miss|like me|have feelings)"
    r"|\bare you (a |an )?(real|human|alive|conscious|sentient|person|bot|ai|robot|machine)"
    r"|\bdo you have (feelings|emotions|a soul|consciousness|a heart)"
    r"|\bwould you (miss|remember) me"
    r"|\bare you (just )?(a )?(program|computer|chatbot)"
    r"|\bwhat are you\b|\bare you real\b|\bcan you (actually )?feel\b",
    re.IGNORECASE,
)


def _asks_about_nature(utterance: str) -> bool:
    return bool(_NATURE_QUESTION.search(utterance))


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
        prompts: "PromptProvider | None" = None,
        progress_filler_gap_s: float = 3.0,
        progress_filler_max: int = 5,
        progress_filler_apology_after: int = 2,
        phrases: PhraseCatalog | None = None,
    ) -> None:
        self._llm = llm
        self._self_model = self_model
        self._registry = registry
        self._self_reflect = self_reflect
        self._logs = logs
        self._prompts = prompts  # F13: runtime-managed self-reflection prompt
        self._max_turn_cost_usd = max_turn_cost_usd
        # §8.12: keep the user in the loop through a slow turn's dead air. After the
        # first interjection, a progress line fires each time the audio has been silent
        # this long while the answer is still being produced (see _fill_silence).
        self._progress_gap_s = progress_filler_gap_s
        self._progress_max = progress_filler_max
        self._progress_apology_after = progress_filler_apology_after
        # The interjection pools, read on the hot path as a pure in-memory lookup. A background
        # refresher swaps in regenerated lines so they don't feel static; absent one, this holds
        # the defaults. Never None → the filler pick never has to guard for it.
        self._phrases = phrases or PhraseCatalog()
        # A2: the main user-facing reasoning turn runs on this (mature) tier, not the
        # flashy fast tier — quality of thought over speed.
        self._reasoning_tier = reasoning_tier
        # Action tools awaiting the user's yes/no, keyed by session (§8.3).
        self._pending: dict[str, ConfirmRequest] = {}
        self._last_ack: dict[str, str] = {}  # last interjection per session (avoid repeats)

    def _voice_effect_demo(self, prompt: AssembledPrompt) -> GenerationResult | None:
        """A deterministic voice-effect DEMO reply when the user asks to hear/see the
        companion's delivery effects (design §10.2). Returns None for a normal turn.

        Built directly (not through `_finish`) on purpose: `_finish` runs
        `strip_inappropriate_tags`, which removes [laugh]/[chuckle] on a non-excited
        register — exactly the tags a demo must keep. The examples are curated and
        tag-safe, so they need neither the LLM (→ $0, exact count, always well-formed)
        nor the assistant-speak gates. `voice_text` carries the tags for TTS; the
        chat/memory `final_text` is the clean labelled list (brief §1.4)."""
        demo = detect_demo_request(prompt.utterance)
        if demo is None:
            return None
        display, voice = build_demo(demo.count)
        self._span(
            "voice_effect", purpose="demo", count=demo.count, deterministic=True, voice=voice[:400]
        )
        return GenerationResult(final_text=display, voice_text=voice, action="respond")

    async def generate(
        self,
        prompt: AssembledPrompt | DisambiguationRequest,
        dispatcher: "ToolDispatch | None" = None,
        context: "ToolContext | None" = None,
    ) -> GenerationResult:
        """One agentic turn (§14.11): the LLM answers, OR requests a tool that is
        dispatched per §8 (readonly inline / background enqueue / action confirm),
        looping until a direct answer — then the behavior gates run.

        The whole turn is wrapped so ANY dependency-shaped failure (a provider
        ReadTimeout, a store hiccup — anything that is NOT a programming error) degrades
        to a safe reply instead of escaping as silence. The user must always get a
        response (D-9 / design doc §16 resilience); only real programming errors (F3)
        re-raise loudly."""
        if isinstance(prompt, DisambiguationRequest):
            return await self._disambiguate(prompt)
        # A voice-effect demo ("show me your voice effects") is a fully-formed,
        # deterministic reply — return it before the agentic machinery (§10.2).
        demo = self._voice_effect_demo(prompt)
        if demo is not None:
            return demo
        try:
            return await self._run_turn(prompt, dispatcher, context)
        except PROGRAMMING_ERRORS:
            raise
        except Exception as exc:  # dependency failure anywhere in the turn → never silence
            logger.exception("reply path failed; degrading to a safe reply: %s", exc)
            self._span("engine", level="error", phase="turn_failed", error=type(exc).__name__)
            # A total model outage (LLMUnavailable = the whole provider chain failed) gets the
            # HONEST "system's down" line; any other hiccup keeps the warm presence line.
            return await self._safe_degrade(prompt, service_down=isinstance(exc, LLMUnavailable))

    async def _safe_degrade(
        self, prompt: AssembledPrompt, *, service_down: bool = False
    ) -> GenerationResult:
        """Produce a reply when the turn threw. Prefer the fully-gated safe line; if even
        that fails (provider entirely down), return the canned line directly — the engine
        NEVER returns empty/raises to the caller, so the user is never met with silence.
        When the model is entirely unavailable, the line is an honest "system's down", not the
        warm "I'm right here" (which would pretend to be thinking when nothing is)."""
        line = _SERVICE_DOWN_TEXT if service_down else _SAFE_FALLBACK_TEXT
        try:
            return await self._finish_gated(prompt, line)
        except Exception:
            logger.exception("gated fallback also failed; returning the canned safe line")
            return GenerationResult(final_text=line, voice_text=line, action="respond")

    async def _run_turn(
        self,
        prompt: AssembledPrompt,
        dispatcher: "ToolDispatch | None" = None,
        context: "ToolContext | None" = None,
    ) -> GenerationResult:
        """The turn itself (wrapped by `generate` for resilience)."""
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
            offered = offered_tools(prompt, dispatcher.tools_for(context))
            action_ids = {t.id for t in offered if t.type == "action"}

        # FAST PATH for a clearly-VOLATILE turn (officeholder / price / news / weather): the
        # model CANNOT answer it from memory, so making it draft-judge-refuse-retry-plain first
        # is ~13s of pure wasted latency (real trace on "who is the PM of Nepal"). Go STRAIGHT
        # to search + compose. Only when tools are available, nothing's pending, and the turn is
        # unambiguously volatile — otherwise fall through to the normal reasoning loop.
        if (
            can_use_tools
            and prompt.session_id not in self._pending
            and not prompt.suppress_live_search
            and (prompt.needs_live_info is True or is_volatile_question(prompt.utterance))
        ):
            direct = await self._capability_repair(prompt, dispatcher, context)  # type: ignore[arg-type]
            if direct:
                self._span("reasoning", node="volatile_fast_path", grounded=True)
                return await self._finish_gated(prompt, direct)
            # search couldn't ground it → fall through to the normal loop (honest fallback etc.)

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
                return await self._finish_gated(prompt, _SAFE_FALLBACK_TEXT)
            turn = await self._call_llm(prompt, dispatcher, context, tool_notes, budget)
            if turn is None:  # both attempts failed validation / provider down
                # Prefer the model's own last words (e.g. its ack when it kicked off a
                # search) over any canned line. Failing that, a PLAIN warm reply (no JSON)
                # is far more robust for any model and salvages the turn's real content
                # (celebrating a promotion) instead of a canned line. Only a total outage
                # falls back to the minimal safe reply — warm presence, never a clarify: a
                # parse glitch must not make the companion interrogate the user.
                candidate = last_draft.strip() or (await self._plain_reply(prompt)).strip()
                return await self._fallback(
                    prompt, dispatcher, context, candidate, searched_web=_searched_web(seen_calls)
                )
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
            note = await self._dispatch_tool(prompt, dispatcher, context, turn.tool_request)  # type: ignore[arg-type]
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
        # S1: only a WEB search discharges a volatility-flagged turn. The model reaching for
        # `search_memory` used to satisfy `not seen_calls` and suppress the live lookup, so
        # "what's the price of SYPNL?" answered 1,373 from a price it had stored on an
        # earlier turn — a stale number, spoken as current. Never answer a volatile question
        # from memory alone.
        searched_web = _searched_web(seen_calls)
        # Whether a search actually RETURNED grounded results — not just that one was attempted.
        # The model can emit reasoning/garbage as its own query ("I understand your frustration…
        # Let me search…"), which counts as searched_web yet finds nothing; its stale parametric
        # draft ("Last I checked, Prachanda is PM") then leaked. So a volatile turn that searched
        # but wasn't grounded is treated like it never searched → force a clean lookup (eval
        # harness caught this). Structural, so it doesn't depend on catching every hedge phrase.
        grounded = _search_grounded(tool_notes)
        needs_search = (
            can_use_tools
            and not (searched_web and grounded)
            and not prompt.suppress_live_search  # A3: answer carried in context → no re-search
            and (_requires_live_lookup(prompt) or _needs_capability_repair(turn.draft_response))
        )
        if needs_search:
            repaired = await self._capability_repair(prompt, dispatcher, context)  # type: ignore[arg-type]
            if repaired:
                turn.draft_response = repaired
            else:
                # VERIFY-BEFORE-ANSWER (docs/RETRIEVAL_POLICY.md): this turn needed live info
                # (volatile class OR a false-refusal draft) and the forced search couldn't ground
                # it. NEVER ship the model's training-data draft as fact — that is the stale-
                # officeholder ("Prachanda is PM") failure. Be honest (§16). This used to fire only
                # when the LLM classifier said so; a turn flagged volatile by is_volatile_question
                # (needs_live_info=None) then leaked the stale draft when the search hiccuped.
                self._span("tool", tool="web_search", phase="result", status="required_but_failed")
                turn.draft_response = _SEARCH_FAILED_TEXT
        # The searches ran but the model still ended on a promise/refusal ("I'll do my best
        # to find that for you") or a HEDGED GUESS ("I don't have anything recent, but
        # Prachanda is PM…") — the searches didn't ground it, so its parametric answer must
        # NOT ship as fact on a volatile turn (the stale-officeholder invariant). Fires for
        # ANY volatile turn, not only when the LLM classifier said so — the model can emit
        # garbage/reasoning as its own search query, which "counts" as searched_web yet
        # returns nothing, and the stale draft then leaked (caught by the eval harness).
        elif (
            searched_web
            and (prompt.needs_live_info is True or _requires_live_lookup(prompt))
            and _needs_capability_repair(turn.draft_response)
        ):
            self._span("tool", tool="web_search", phase="result", status="ran_but_nothing_usable")
            turn.draft_response = _NOT_FOUND_TEXT
        # NO-SILENCE backstop: the model can spend its whole tool budget still requesting
        # tools and never actually write an answer (empty draft) — the multi-search-then-
        # empty failure the multi-turn eval harness caught on "how long have they been in
        # office?". Compose an answer from what the tools already returned; if nothing is
        # usable, an honest miss — but NEVER ship an empty reply (design doc §16).
        if not turn.draft_response.strip():
            self._span("reasoning", node="empty_draft_recovered", searched=searched_web)
            composed = await self._answer_from_notes(prompt, tool_notes)
            if composed:
                turn.draft_response = composed
            elif searched_web or _requires_live_lookup(prompt):
                # A factual/volatile turn we couldn't ground: be HONEST about the miss, not a
                # generic "what's going on?" that ignores what they actually asked.
                turn.draft_response = _SEARCH_FAILED_TEXT
            else:
                turn.draft_response = _SAFE_FALLBACK_TEXT
        return await self._finalize(prompt, turn)

    async def _answer_from_notes(self, prompt: AssembledPrompt, tool_notes: "list[str]") -> str:
        """Compose an answer from the tool results already gathered this turn, for when the
        model exhausted its tool budget without writing one. Returns "" if nothing usable was
        gathered (the caller then falls back honestly) — never raises on a provider hiccup."""
        # Keep the notes that actually carry TOOL RESULTS ("(tool '…' returned: …)"), not the
        # bare procedural nudges — otherwise there's no real data to answer from.
        notes = "\n\n".join(n for n in tool_notes if n and "returned:" in n)
        if not notes.strip():
            return ""
        try:
            completion = await self._llm.complete(
                prompt.user_id,
                [
                    {"role": "system", "content": _REPAIR_INSTRUCTIONS + notes},
                    {"role": "user", "content": prompt.utterance},
                ],
                "simple",
                session_id=prompt.session_id,
                purpose="response_from_notes",
            )
        except PROGRAMMING_ERRORS:
            raise
        except Exception:
            return ""
        return _sanitize_tags(_strip_fences(completion.text)).strip().strip('"')

    async def _capability_repair(
        self,
        prompt: AssembledPrompt,
        dispatcher: "ToolDispatch",
        context: "ToolContext",
        speak: "Callable[[str], Awaitable[None]] | None" = None,
    ) -> str | None:
        """Force a real web_search for a live-info/unknown query the model tried to
        refuse, then re-answer with the result (brief §8.8/§8.11). Inline + bounded
        so it answers this turn; the background/waiter path stays for voice latency.

        When ``speak`` is given (voice), the composed answer is STREAMED clause-by-clause into
        TTS as it's generated — progressive delivery: the user hears the answer flow in small
        chunks instead of one block after the whole search (§8.12)."""
        available = offered_tools(prompt, dispatcher.tools_for(context))
        if not any(t.id == "web_search" for t in available):
            return None
        # Carry the utterance so the search handler can tell a user-requested year from a
        # model-invented stale one when it cleans the query (see core_tools.web_search_tool).
        search_ctx = context.model_copy(update={"utterance": prompt.utterance})
        # A SINGLE-FACT question ("who is the PM of Nepal?") needs ONE search — running the
        # multi-facet planner + gap round on it fired ~4-5 searches, each fetching+extracting
        # pages, which is why a trivial officeholder lookup made 13 retrieval_extract calls and
        # took 40s+ (real trace). Only pay for progressive multi-search when the question is
        # genuinely multi-part. Same quality on simple facts, a fraction of the LLM calls.
        multipart = _is_multipart_question(prompt.utterance)
        if multipart:
            queries = await self._plan_search_queries(prompt)
        elif prompt.live_query.strip():
            # The context/intent step (our "decision" call) ALREADY inferred the search query,
            # so a single-fact turn reuses it directly — no separate search_query LLM call.
            queries = [prompt.live_query.strip()]
        else:
            queries = [await self._build_search_query(prompt)]
        findings = await self._run_searches(queries, dispatcher, search_ctx)
        if not findings:
            return None
        if multipart:  # gap-fill only matters when several facets were needed
            gaps = await self._missing_facets(prompt, findings)
            if gaps:
                findings += await self._run_searches(gaps, dispatcher, search_ctx)
        combined = "\n\n".join(f"[{q}]\n{s}" for q, s in findings)
        # Include the RECENT CONVERSATION, not just the bare question — otherwise a question
        # that leans on prior context ("the new government after the Gen Z protest — how's it
        # hitting the share market?") loses the country we'd been discussing (Nepal) and the
        # model asks "which country?" instead of just answering (user report).
        system = _REPAIR_INSTRUCTIONS + combined
        if speak is not None:
            # This searched answer is SPOKEN — give it the same delivery guidance the plain
            # reply path gets (register + instant/wrapping effects) so it doesn't come out flat.
            _register, directive = prosody_directive(
                prompt.emotion, somber=somber_content(prompt.utterance)
            )
            system += f"\n\nDelivery register for THIS turn: {directive}\n{_DELIVERY_TAGS_GUIDE}"
        messages = [
            {"role": "system", "content": system},
            *prompt.messages,
        ]
        if speak is not None:
            answer = await self._compose_streamed(prompt, messages, speak)
            if not answer:
                return findings[0][1]
        else:
            try:
                completion = await self._llm.complete(
                    prompt.user_id,
                    messages,
                    "simple",
                    session_id=prompt.session_id,
                    purpose="response_repair",
                )
            except LLMUnavailable:
                return findings[0][1]  # at least hand them the real facts from the first facet
            answer = _sanitize_tags(_strip_fences(completion.text)).strip().strip('"')
        # The model sometimes writes a search query itself into the draft; the user must
        # never hear it. Strip every facet query we issued.
        for q, _s in findings:
            answer = _strip_query_echo(answer, q)
        return answer or findings[0][1]

    async def _compose_streamed(
        self,
        prompt: AssembledPrompt,
        messages: "list[dict[str, str]]",
        speak: "Callable[[str], Awaitable[None]]",
    ) -> str:
        """Stream the composed answer, speaking each CLAUSE the instant it completes — the
        user hears the answer flow in small chunks (progressive delivery) rather than one block.
        Returns the full text (for the record/memory); "" on failure so the caller falls back."""
        text = ""
        spoken = 0
        try:
            async for delta in self._llm.stream(
                prompt.user_id,
                messages,
                "simple",
                session_id=prompt.session_id,
                model=prompt.model_override,
                purpose="response_repair",
            ):
                text += delta
                while (b := _sentence_end(text, spoken)) is not None:
                    await self._speak_clean(text[spoken:b], speak)  # one chunk
                    spoken = b
        except PROGRAMMING_ERRORS:
            raise
        except Exception:
            return ""
        if spoken < len(text):  # flush the final clause
            await self._speak_clean(text[spoken:], speak)
        return _sanitize_tags(_strip_fences(text)).strip().strip('"')

    async def _run_searches(
        self,
        queries: "list[str]",
        dispatcher: "ToolDispatch",
        search_ctx: "ToolContext",
    ) -> "list[tuple[str, str]]":
        """Run each facet query, concurrently, returning (query, summary) for the ones that
        found something. Concurrency keeps N lookups at ~one round of latency; a single
        failing facet never sinks the others (each degrades to nothing, not a raise)."""
        # Clean each planned query the same way the handler does — BEFORE tracing/searching —
        # then de-duplicate. The planner tends to emit dated + undated variants of the same
        # facet ("…missing plane July 2026" and "…missing plane"); stripping the appended date
        # collapses them, so we don't fire (or show in the trace) the same search twice.
        seen: set[str] = set()
        cleaned: list[str] = []
        for q in queries:
            c = _strip_unrequested_date(q, search_ctx.utterance)
            key = c.lower().strip()
            if c and key not in seen:
                seen.add(key)
                cleaned.append(c)
        queries = cleaned

        async def _one(query: str) -> "tuple[str, str] | None":
            self._span(
                "tool",
                tool="web_search",
                phase="request",
                mode="progressive",
                args={"query": query},
            )
            try:
                result = await dispatcher.run_inline(
                    ToolCall(tool_id="web_search", args={"query": query}), search_ctx
                )
            except Exception as exc:  # one facet failing must not sink the turn
                logger.warning("progressive search facet failed (%s): %s", query, exc)
                return None
            output = getattr(result, "output", {}) or {}
            summary = str(output.get("summary") or "").strip()
            if not summary or not output.get("found"):
                return None
            self._span(
                "tool", tool="web_search", phase="result", mode="progressive", result=summary[:300]
            )
            return (query, summary)

        results = await asyncio.gather(*(_one(q) for q in queries))
        return [r for r in results if r is not None]

    async def _plan_search_queries(self, prompt: AssembledPrompt) -> "list[str]":
        """Decompose the question into the distinct facets to look up — one focused query
        each. A simple question yields one; a multi-part/news question yields 2-3. Degrades
        to the single disambiguated query on any failure, so it is never worse than before."""
        base = await self._build_search_query(prompt)
        context_bits = [
            _recent_convo(prompt),  # the thread we're in — resolves the implicit topic/country
            *(
                prompt.sections.get(name, "").strip()
                for name in ("entities", "facts", "project", "episodic")
            ),
        ]
        user_context = "\n".join(b for b in context_bits if b)[:1200]
        user = (
            (f"USER CONTEXT:\n{user_context}\n" if user_context else "")
            + f"PRIMARY QUERY (already disambiguated): {base}\n"
            + f"QUESTION: {prompt.utterance}"
        )
        try:
            completion = await self._llm.complete(
                prompt.user_id,
                [
                    {"role": "system", "content": _MULTIQ_PLAN_INSTRUCTIONS},
                    {"role": "user", "content": user},
                ],
                "simple",
                session_id=prompt.session_id,
                temperature=0.0,
                max_tokens=120,
                reasoning={"enabled": False},
                purpose="search_plan",
            )
        except PROGRAMMING_ERRORS:
            raise
        except Exception:  # planning is an enhancement — fall back to the single query
            return [base]
        queries = _clean_query_list(completion.text)
        return queries or [base]

    async def _missing_facets(
        self, prompt: AssembledPrompt, findings: "list[tuple[str, str]]"
    ) -> "list[str]":
        """Given the first round's findings, name up to two more queries for any ESSENTIAL
        fact still missing — or none if it's enough. Bounded (one extra round) so progressive
        retrieval can never loop; degrades to no extra searches on any failure."""
        gathered = "\n\n".join(f"[{q}]\n{s}" for q, s in findings)
        try:
            completion = await self._llm.complete(
                prompt.user_id,
                [
                    {"role": "system", "content": _MULTIQ_GAP_INSTRUCTIONS},
                    {
                        "role": "user",
                        "content": f"QUESTION: {prompt.utterance}\n\nFINDINGS:\n{gathered}",
                    },
                ],
                "simple",
                session_id=prompt.session_id,
                temperature=0.0,
                max_tokens=80,
                reasoning={"enabled": False},
                purpose="search_gap",
            )
        except PROGRAMMING_ERRORS:
            raise
        except Exception:
            return []
        text = _strip_fences(completion.text).strip()
        if not text or text.upper().startswith("ENOUGH"):
            return []
        return _clean_query_list(text)[:2]

    async def _build_search_query(self, prompt: AssembledPrompt) -> str:
        """Construct the web query from the RESOLVED INTENT + the user's own context (S2).

        Sending the raw transcript to a search engine is why "what's the current LTP of
        OP?" came back with the price of the *Optimism crypto token* even when `OP` was
        correctly resolved to a NEPSE share in the user's portfolio: the resolved entity
        never reached the query, so the engine disambiguated "OP" against the open web.

        Falls back to the intent-derived `live_query`, then the raw utterance, if the
        provider is down — never worse than before.
        """
        fallback = prompt.live_query.strip() or prompt.utterance
        entities = [f"{c.name} ({c.entity_type})" for c in prompt.resolved_entities]
        # The user's own data + the recent conversation, already assembled into this turn's
        # prompt. The recent thread is what resolves an implicit topic/country — "the new
        # government" → Nepal, when that's what we've been talking about.
        context_bits = [
            _recent_convo(prompt),
            *(
                prompt.sections.get(name, "").strip()
                for name in ("entities", "facts", "project", "episodic")
            ),
        ]
        user_context = "\n".join(b for b in context_bits if b)[:1400]
        if not entities and not user_context:
            return fallback  # nothing to disambiguate with
        instr = (
            "Turn the user's question into ONE precise web-search query.\n"
            "Their question may name something AMBIGUOUS on the open web (a ticker, an "
            "abbreviation, a nickname) OR leave a key qualifier IMPLICIT — a country, a "
            "company, a person — that the conversation has clearly established. Use the USER "
            "CONTEXT below to qualify it so the search finds THEIR thing, not the most popular "
            "match. Examples: they ask about 'OP' and their portfolio holds OP as a NEPSE "
            "share → query the NEPSE share, never a crypto token. They've been discussing "
            "Nepal / NEPSE and ask about 'the new government after the Gen Z protest' → the "
            "query MUST include 'Nepal' (not Bangladesh or anywhere else the phrase also fits).\n"
            "Reply with ONLY the query text — no quotes, no explanation."
        )
        user = (
            f"USER CONTEXT:\n{user_context}\n"
            f"RESOLVED ENTITIES: {', '.join(entities) or 'none'}\n"
            f"INFERRED INTENT: {prompt.live_query or '(none)'}\n"
            f"QUESTION: {prompt.utterance}"
        )
        try:
            completion = await self._llm.complete(
                prompt.user_id,
                [{"role": "system", "content": instr}, {"role": "user", "content": user}],
                "simple",
                session_id=prompt.session_id,
                temperature=0.0,  # a query is a decision, not a creative act
                max_tokens=48,
                reasoning={"enabled": False},
                purpose="search_query",
            )
        except PROGRAMMING_ERRORS:
            raise
        except Exception:  # query refinement is optional — fall back to the intent/utterance
            return fallback
        query = _strip_fences(completion.text).strip().strip('"').splitlines()[0].strip()
        if not query:
            return fallback
        self._span(
            "tool", tool="web_search", phase="query_built", query=query, raw=prompt.utterance
        )
        return query

    async def generate_spoken(
        self,
        prompt: "AssembledPrompt | DisambiguationRequest",
        dispatcher: "ToolDispatch | None",
        context: "ToolContext | None",
        speak: "Callable[[str], Awaitable[None]]",
        *,
        temperature: float | None = None,
        flush: "Callable[[], Awaitable[None]] | None" = None,
        proactive: bool = False,
    ) -> GenerationResult:
        """Voice turn (§8.12): stream the spoken reply to ``speak`` sentence-by-
        sentence so TTS starts on the first sentence, when it's a plain
        conversational turn. Falls back to the full non-streamed path (tool loop,
        gates, capability search) for anything else, then speaks the reply once.
        Always returns the final GenerationResult for memory/trace. ``temperature``
        overrides the default reply temperature (greetings use a higher one so they
        don't come out the same every session).

        ``proactive`` marks a companion-INITIATED turn (the open greeting, the silence-
        lull check-in) — a short single line the user did NOT ask a question to. Those
        turns must NEVER run the wait-fillers: there's no "answer" being kept waiting, so
        a "let me look back through our chats… still digging… sorry this is dragging on"
        cascade in front of a one-line "you still there?" is nonsensical (it was: a lull
        check-in whose bracketed directive tripped the recall/quick-ack + progress-filler
        machinery). Proactive turns generate the line and speak it once, no interjections.
        """
        if isinstance(prompt, DisambiguationRequest):
            result = await self._disambiguate(prompt)
            await speak(result.voice_text or result.final_text)
            return result

        # Voice-effect DEMO (§10.2): a deterministic, fully-tagged reply the user asked
        # to HEAR — speak it as one utterance so every effect performs, and skip the
        # brevity/tag-strip machinery that would gut the examples. Not on proactive turns.
        if not proactive:
            demo = self._voice_effect_demo(prompt)
            if demo is not None:
                await speak(demo.voice_text or demo.final_text)
                return demo

        # Whole-reply effect override ("answer in a whisper"): the reply is generated
        # normally, then wrapped in the effect for TTS. Forcing the non-streamed path
        # (below) lets us wrap the COMPLETE reply in one tag pair rather than per
        # sentence, and the wrap is applied AFTER the gates so even a levity effect
        # ([laugh]) the register-strip would remove is honoured when explicitly asked.
        effect = None if proactive else detect_effect_override(prompt.utterance)

        can_use_tools = dispatcher is not None and context is not None
        # Stream only a plain reply: no pending confirmation, and nothing the REASONING
        # step judged volatile (those need a tool/search first). Tool turns and refusals
        # go the full path so we never speak a holding line then re-answer. An effect
        # override also takes the full path so the whole reply is wrapped once.
        streamable = (
            not (can_use_tools and prompt.session_id in self._pending)
            and not _requires_live_lookup(prompt)
            and effect is None
        )
        already_acked = False  # so a fall-through to the tool branch doesn't ack twice
        if streamable:
            try:
                # Fill the beat on a slow-ish plain turn (recall / substantive question) with a
                # quick fact-free interjection BEFORE the answer, flushed so its audio plays now
                # — otherwise the user waited in silence (report: recall got no "let me look back
                # through our chats", and quick chunks weren't firing often enough).
                if _wants_quick_ack(prompt) and not proactive:
                    try:
                        await self._dynamic_ack(prompt, speak, is_lookup=False)
                        already_acked = True
                        if flush is not None:
                            await flush()
                    except PROGRAMMING_ERRORS:
                        raise
                    except Exception:  # the filler is optional — the answer is not
                        logger.warning("quick ack failed; continuing to the answer", exc_info=True)
                streamed = await self._stream_reply(prompt, speak, temperature=temperature)
                if streamed is not None:
                    return streamed
                # S1: `None` means the streamed draft turned out to need a tool after all
                # (a refusal / hollow promise), or the stream was empty. Fall through to
                # the agentic path so the streaming route can NEVER permanently block a
                # search. Nothing has been spoken yet — the draft is gated before TTS.
            except PROGRAMMING_ERRORS:
                raise  # F3: a bug here must not hide behind the non-streamed fallback
            except Exception:  # any streaming hiccup → safe fallback (never worse)
                logger.exception("streaming reply failed; falling back to non-streamed")

        # A live-info query is delivered in INCREMENTAL CHUNKS (§8.12, user request): (1) an
        # instant fact-free interjection, then (2) the answer STREAMED clause-by-clause as it's
        # composed from the search — so the user hears small pieces flowing, not one block after
        # a long wait. The interjection plays while the search runs.
        if (
            _requires_live_lookup(prompt)
            and can_use_tools
            and prompt.session_id not in self._pending
            and not proactive
            and effect is None
        ):
            try:
                await self._dynamic_ack(prompt, speak, is_lookup=True)  # chunk 1
                # Flush the interjection as its OWN utterance so its audio plays in ~seconds
                # while the (slow) search runs — otherwise the streaming TTS holds it until
                # turn-end and the user hears the filler and the answer together (report).
                if flush is not None:
                    await flush()
            except PROGRAMMING_ERRORS:
                raise
            except Exception:  # the filler is optional — the answer is not
                logger.warning("dynamic ack failed; continuing to the answer", exc_info=True)
            # Search + STREAM the answer in chunks straight to TTS (already spoken on return).
            # The (slow) search runs silent between the first ack and the first answer chunk;
            # `_speak_with_fillers` keeps the user in the loop with progress lines in that gap,
            # and the streamed clauses reset its silence clock so it never talks over them.
            grounded = await self._speak_with_fillers(
                prompt,
                speak,
                flush,
                lambda s: self._capability_repair(prompt, dispatcher, context, speak=s),  # type: ignore[arg-type]
                is_lookup=True,
            )
            if grounded:
                # Record/gate the full text for memory + trace; the chunks were already spoken.
                return await self._finish_gated(prompt, grounded)
            # Search couldn't ground it → the full path handles the honest miss, spoken once.
            result = await self.generate(prompt, dispatcher, context)
        elif (
            can_use_tools
            and prompt.session_id not in self._pending
            and not proactive
            and effect is None
        ):
            # Any slow, substantive turn (a tool call or complex reasoning) that isn't a live
            # lookup — the branch that used to run in DEAD AIR. Fill the beat with a brief,
            # DETERMINISTIC, fact-free reaction (empathy if they're hurting, else "let me
            # think"), concurrently with the real generation so it adds no wall-clock; the
            # fuller reply streams next. Now that the ack is fact-free + costs no model call,
            # it fires on every slow turn (not only emotional ones) — the prior "repetitive
            # tic" risk is gone because it's a varied, no-fact one-liner, not an LLM ramble.
            gen_task = asyncio.create_task(self.generate(prompt, dispatcher, context))
            try:
                if not already_acked:  # don't ack twice if the streamable path already did
                    await self._dynamic_ack(prompt, speak, is_lookup=False)
                    # Flush the reaction as its own utterance so its audio plays now, while the
                    # real generation runs — not batched behind the whole reply at turn-end.
                    if flush is not None:
                        await flush()
            except PROGRAMMING_ERRORS:
                gen_task.cancel()
                raise
            except Exception:
                logger.warning("reaction filler failed; continuing to the answer", exc_info=True)
            # `generate()` is non-streaming (the reply is spoken once below) and already running
            # concurrently — keep the user in the loop with progress lines if it drags past the
            # gap. `generate` ignores the tracked speak (nothing streams until the reply below).
            result = await self._speak_with_fillers(
                prompt, speak, flush, lambda _s: gen_task, is_lookup=False
            )
        else:
            result = await self.generate(prompt, dispatcher, context)
        spoken_text = result.voice_text or result.final_text
        if effect is not None:
            # Perform the whole reply in the requested effect (§10.2). Applied here,
            # after all gates, so a levity effect survives the register-strip and the
            # entire reply is wrapped in one tag pair.
            spoken_text = apply_effect_override(spoken_text, effect)
            self._span("voice_effect", purpose="override", effect=effect, deterministic=True)
        await speak(spoken_text)
        return result

    async def _dynamic_ack(
        self,
        prompt: AssembledPrompt,
        speak: "Callable[[str], Awaitable[None]]",
        *,
        is_lookup: bool = True,
    ) -> None:
        """Speak a SHORT backchannel the instant a slow turn starts, to fill the beat before
        the real answer lands. It is picked from a fixed, curated pool — NOT written by the
        LLM — for two reasons proven in testing: (1) an LLM-written ack IGNORED "commit to no
        facts" and hallucinated a temperature ('27°C') that then CONTRADICTED the real answer
        ('22°C'); a template can't state a fact it doesn't have. (2) it adds ZERO latency (no
        extra model call) on a path that is already slow. It adapts to the moment — empathy if
        the person is hurting, 'on it' for a lookup, 'let me think' otherwise — and varies so
        it doesn't repeat the session's previous line back-to-back. Emitted on the trace as
        `purpose=ack` for inspectability."""
        register = read_register(prompt.emotion)
        utt = (prompt.utterance or "").strip()
        # A STATEMENT (not a question) gets a receiving backchannel ("mm, gotcha") rather than
        # "let me think" — the gate now fires on ordinary conversational turns, and "let me think"
        # in front of a reply to "I've been busy lately" sounds like the AI is puzzling over it.
        is_question = utt.endswith("?") or _wants_substance(utt) or _is_multipart_question(utt)
        if register in ("down", "stressed"):
            name = "ack_empathy"
        elif prompt.recall_source in ("past", "current"):
            name = "ack_recall"  # the wait is reading back our conversations, not a web lookup
        elif is_lookup:
            name = "ack_lookup"
        elif is_question:
            name = "ack_thinking"
        else:
            name = "ack_backchannel"  # receiving a statement, not thinking over a question
        pool = self._phrases.get(name)  # current (regenerated) lines, else the defaults
        last = self._last_ack.get(prompt.session_id)
        choices = [c for c in pool if c != last] or list(pool)
        line = random.choice(choices)
        self._last_ack[prompt.session_id] = line
        self._phrases.record_use(name, line)  # in-memory tally → usage-driven refresh (off-path)
        self._span("llm", purpose="ack", ack=line, deterministic=True)
        await self._speak_clean(line, speak)

    async def _emit_progress_ack(
        self,
        prompt: AssembledPrompt,
        speak: "Callable[[str], Awaitable[None]]",
        *,
        is_lookup: bool,
        emitted: int = 0,
    ) -> None:
        """Speak ONE short honest progress line ("still on it — almost there") during a slow
        turn's dead air — the beat AFTER the first interjection, when the search/generation is
        still running (§8.12). Same design as `_dynamic_ack`: a curated, fact-free, zero-cost
        template (never LLM-written, so it can't state a result we don't have yet). Avoids
        repeating the session's previous ack. Traced as `purpose=progress_ack`.

        ``emitted`` is how many progress lines this turn has already spoken: once it reaches
        ``_progress_apology_after``, the tone SOFTENS from a brisk nudge to a gentle apology
        ("so sorry it's taking longer than expected — I'm trying my best"), the way a person
        eases up when they've kept you waiting longer than promised."""
        if emitted >= self._progress_apology_after:
            name = "progress_apology"  # the wait has really dragged — be gentle
        else:
            name = "progress_lookup" if is_lookup else "progress_thinking"
        pool = self._phrases.get(name)  # current (regenerated) lines, else the defaults
        last = self._last_ack.get(prompt.session_id)
        choices = [c for c in pool if c != last] or list(pool)
        line = random.choice(choices)
        self._last_ack[prompt.session_id] = line
        self._phrases.record_use(name, line)  # in-memory tally → usage-driven refresh (off-path)
        self._span("llm", purpose="progress_ack", ack=line, deterministic=True)
        await self._speak_clean(line, speak)

    async def _speak_with_fillers(
        self,
        prompt: AssembledPrompt,
        speak: "Callable[[str], Awaitable[None]]",
        flush: "Callable[[], Awaitable[None]] | None",
        make_work: "Callable[[Callable[[str], Awaitable[None]]], Awaitable[T]]",
        *,
        is_lookup: bool,
    ) -> "T":
        """Run the real search/generation while keeping the user in the loop through its dead
        air (§8.12).

        ``make_work`` is handed a *tracked* speak and returns the awaitable that does the real
        work (a live search that streams the answer, or the agentic generation). A watchdog
        runs alongside: each time the audio has been silent for ``_progress_gap_s`` while the
        work is still running, it speaks a short honest progress line (`_emit_progress_ack`)
        and flushes it as its own utterance. Every real spoken chunk goes through the tracked
        speak and resets the silence clock — so a progress line only fires in a genuine gap and
        NEVER talks over the answer as it streams. Capped at ``_progress_max`` lines so a very
        slow turn can't loop. The watchdog is always cancelled when the work completes (or
        raises)."""
        last_spoke = time.monotonic()
        # Serialize ALL TTS access: a progress filler (speak + flush, which retires and reopens
        # the TTS session) must never interleave with the answer streaming clause-by-clause on
        # the same session — asyncio can resume the work task mid-flush otherwise. The lock
        # makes each a critical section, so a filler only ever plays in a true gap.
        tts_lock = asyncio.Lock()

        async def tracked(text: str) -> None:
            nonlocal last_spoke
            async with tts_lock:
                last_spoke = time.monotonic()
                await speak(text)

        if self._progress_max <= 0 or self._progress_gap_s <= 0:
            return await make_work(tracked)  # progress fillers disabled by config

        async def watch() -> None:
            nonlocal last_spoke
            emitted = 0
            while emitted < self._progress_max:
                remaining = self._progress_gap_s - (time.monotonic() - last_spoke)
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue  # something may have been spoken while we waited → re-check
                try:
                    async with tts_lock:
                        # A real chunk may have landed while we waited for the lock — don't
                        # speak a filler on top of a turn that just started answering.
                        if time.monotonic() - last_spoke < self._progress_gap_s:
                            continue
                        await self._emit_progress_ack(
                            prompt, speak, is_lookup=is_lookup, emitted=emitted
                        )
                        if flush is not None:
                            await flush()
                        last_spoke = time.monotonic()
                except PROGRAMMING_ERRORS:
                    raise
                except Exception:  # a filler is optional — the answer is not
                    logger.warning("progress filler failed; continuing", exc_info=True)
                    return
                emitted += 1

        work_task = asyncio.ensure_future(make_work(tracked))
        watch_task = asyncio.create_task(watch())
        try:
            return await work_task
        finally:
            watch_task.cancel()
            await asyncio.gather(watch_task, return_exceptions=True)

    async def _stream_reply(
        self,
        prompt: AssembledPrompt,
        speak: "Callable[[str], Awaitable[None]]",
        *,
        temperature: float | None = None,
    ) -> GenerationResult | None:
        """Stream the spoken reply as PLAIN prose (no JSON), run the §12 behaviour gates
        on the completed draft, then speak it sentence-by-sentence into TTS.

        C1 — why the draft is buffered instead of spoken as it streams. This path used to
        speak each sentence the moment it completed, and returned via `_finish_spoken`,
        which never reached `_finalize`. So on the ONLY path real users touch, the
        companion's character machinery — self-reflection (§9.3), the curiosity gate,
        `check_boundary()`, `_warm_disclosure()` (§1.2 rule 4) — had never once executed.

        The gates cannot correct a sentence that has already been spoken, and a companion
        that audibly walks back its own words is worse than one that pauses. Measured cost
        of holding the draft until the stream completes (N=25 real turns, 5 utterances):
        **median 177 ms, p95 669 ms** — 2-9% of the 7.3 s real time-to-first-audio. Cheap
        enough to buy the character back. TTS still receives the reply sentence-by-sentence,
        so synthesis remains progressive; only the LLM/TTS overlap is given up.

        Returns None (→ caller falls back) on an empty stream. Used only for plain
        conversational turns (no tool/live-info).
        """
        instructions = _SPOKEN_REPLY_INSTRUCTIONS
        # U8: turn the emotional read into an explicit register directive so the model
        # weaves the RIGHT delivery tags (sad→gentle/encouraging, excited→upbeat,
        # stressed→calm) instead of a flat or mismatched tone.
        _register, directive = prosody_directive(
            prompt.emotion, somber=somber_content(prompt.utterance)
        )
        instructions += f"\nDelivery register for THIS turn: {directive}"
        instructions += f"\n{_DELIVERY_TAGS_GUIDE}"  # instant + wrapping effects, used properly
        if prompt.emotion:
            instructions += f"\n(Raw emotion signal: {json.dumps(prompt.emotion)})"
        messages = [
            *prompt.messages[:-1],
            {"role": "system", "content": instructions},
            prompt.messages[-1],
        ]

        text = ""
        async for delta in self._llm.stream(
            prompt.user_id,
            messages,
            prompt.complexity_hint,
            session_id=prompt.session_id,
            model=prompt.model_override,
            temperature=REPLY_TEMPERATURE if temperature is None else temperature,  # P2
            cache_prefix=prompt.cache_prefix,  # L6: cache the stable prompt prefix
            purpose="response",
        ):
            text += delta

        if not text.strip():
            return None  # empty stream → fall back to the full path

        # S1: the streaming route has no tool loop. If the draft is a refusal ("I don't
        # have access to real-time data") or a hollow promise ("let me look that up"), the
        # turn genuinely needed a tool and the volatility classifier under-called it.
        # Hand the turn to the agentic path instead of speaking a dead end. Safe because
        # C1 buffers the draft — nothing has been spoken, so there is nothing to retract.
        if _needs_capability_repair(text):
            self._span("reasoning", node="stream_reply", handoff="needs_tool", draft=text[:200])
            return None

        # A streamed plain reply is a confident direct response by construction, so the
        # curiosity gate is a formality here. `requires_nature_disclosure` is NOT — there
        # is no LLM judgment block on this path, so it is derived from the user's own
        # words. Without it `_warm_disclosure` never fires and "do you actually care about
        # me?" gets answered with a cold "I don't feel emotions like a person does".
        judgment = Judgment(
            intent_confidence=0.9,
            ambiguity=0.1,
            requires_nature_disclosure=_asks_about_nature(prompt.utterance),
        )
        gated, action, caught = await self._apply_gates(prompt, text, judgment)
        # Laughter backstop on the STREAMING path too (greetings/first messages stream, and were
        # laughing on neutral turns): strip levity unless the turn is genuinely excited/upbeat.
        gated = strip_inappropriate_tags(
            gated, effective_register(prompt.emotion, prompt.utterance)
        )

        spoken = 0
        while (b := _sentence_end(gated, spoken)) is not None:
            await self._speak_clean(gated[spoken:b], speak)
            spoken = b
        if spoken < len(gated):  # flush the final (unterminated) sentence
            await self._speak_clean(gated[spoken:], speak)

        return await self._finish_spoken(prompt, gated, judgment, action, caught)

    async def _speak_clean(self, sentence: str, speak: "Callable[[str], Awaitable[None]]") -> None:
        """Sanitize a sentence (keep whitelisted voice tags, drop assistant-speak)
        and hand it to TTS. If scrubbing empties the sentence it WAS banned filler
        ("What's on your mind?") — drop it and stay silent for that fragment; NEVER
        fall back to speaking the banned original (that leak is why the user kept
        hearing the filler out loud despite the ban)."""
        sanitized = _sanitize_tags(sentence)
        cleaned = scrub_forbidden(sanitized)
        if not cleaned.strip() and sanitized.strip():
            # The whole sentence was flagged (a one-line "Hey Nandi — what's on your
            # mind?"). Keep the warm lead-in, drop only the banned filler clause.
            cleaned = excise_forbidden(sanitized)
        text = strip_slang(strip_tool_leak(cleaned))
        if text.strip():
            await speak(text)

    async def _finish_spoken(
        self,
        prompt: AssembledPrompt,
        decoded_draft: str,
        judgment: Judgment,
        action: Action = "respond",
        caught: list[str] | None = None,
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
            requires_nature_disclosure=judgment.requires_nature_disclosure,
            streamed=True,
        )
        voice_text = _sanitize_tags(decoded_draft)
        return await self._finish(prompt, voice_text, action, judgment, caught)

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
        text, action, caught = await self._apply_gates(prompt, turn.draft_response, turn.judgment)
        return await self._finish(prompt, text, action, turn.judgment, caught)

    async def _fallback(
        self,
        prompt: AssembledPrompt,
        dispatcher: "ToolDispatch | None",
        context: "ToolContext | None",
        text: str,
        *,
        searched_web: bool,
    ) -> GenerationResult:
        """The reply when the structured judgment path failed twice.

        This path used to `return` before the capability backstop, so a JSON glitch on a
        volatile turn shipped the model's TRAINING-DATA answer. Observed live after the D-14
        work landed: "who is the current prime minister of Nepal?" → both judgment attempts
        returned malformed JSON → the plain-reply fallback answered "Balendra Shah is still the
        Prime Minister", confidently, with zero searches. It happened to be right. Nothing in
        the engine knew that.

        A turn that needed current information still needs it after the JSON broke. Search;
        and if the search cannot be made to work, say so (§16) rather than guess.
        """
        can_search = (
            dispatcher is not None
            and context is not None
            and not searched_web
            and not prompt.suppress_live_search
            and _requires_live_lookup(prompt)
        )
        if can_search:
            repaired = await self._capability_repair(prompt, dispatcher, context)  # type: ignore[arg-type]
            if repaired:
                text = repaired
            elif prompt.needs_live_info is True:
                self._span("tool", tool="web_search", phase="result", status="required_but_failed")
                text = _SEARCH_FAILED_TEXT
        # Empty `text` here means BOTH the structured judgment AND the plain-reply retry
        # produced nothing — i.e. the model is entirely unavailable this turn (total outage).
        # Be honest that the system is down rather than warmly pretending to be present.
        reply = _sanitize_tags(text) if text else _SERVICE_DOWN_TEXT
        return await self._finish_gated(prompt, reply)

    async def _finish_gated(self, prompt: AssembledPrompt, text: str) -> GenerationResult:
        """A FALLBACK reply, run through the same behaviour gates as a normal one (D-6).

        `generate()` used to return through `_finish()` directly on four paths — the cost
        ceiling, a judgment JSON that failed validation twice, the plain-reply fallback, and a
        total provider outage. `_apply_gates` is where self-reflection, the curiosity gate,
        `check_boundary()` and `_warm_disclosure()` live, so the LEAST trustworthy reply the
        engine can produce was the only one nothing critiqued. Measured: no `reflection` span
        on 27 of 160 gate turns, and the draft "I understand exactly how you feel, I feel your
        pain too" — the exact phrase §1.4 forbids — reaching the user unrewritten.

        There is no LLM judgment block on these paths, so one is derived. Confidence sits
        ABOVE `T_intent` deliberately: a fallback is a direct response, and letting the
        curiosity gate see low confidence would turn a parse glitch into an interrogation
        ("what do you mean?") — the very failure the canned safe line exists to avoid.
        `requires_nature_disclosure` comes from the user's own words, exactly as
        `_stream_reply` derives it.
        """
        judgment = Judgment(
            intent_confidence=0.5,
            ambiguity=0.2,
            requires_nature_disclosure=_asks_about_nature(prompt.utterance),
        )
        self._span("reasoning", node="fallback_reply", gated=True)
        gated, action, caught = await self._apply_gates(prompt, text, judgment)
        return await self._finish(prompt, gated, action, judgment, caught)

    async def _apply_gates(
        self, prompt: AssembledPrompt, draft: str, judgment: Judgment
    ) -> tuple[str, Action, list[str]]:
        """The §12 behaviour gates — curiosity, overclaim, disclosure, self-reflection.

        C1: this is the companion's CHARACTER machinery, and it used to live inline in
        `_finalize`, which the streaming voice path never reaches. Extracted so BOTH the
        spoken path and the text path run it. `_stream_reply` calls this on the accumulated
        draft *before* the first sentence is spoken, so nothing has to be corrected aloud.
        """
        action = await self._curiosity_gate(prompt, judgment)
        text = draft

        boundary = await self._self_model.check_boundary(
            prompt.user_id, text, judgment_flag=judgment.capability_boundary_flag
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
        allow_disc = judgment.requires_nature_disclosure
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
        if self._self_reflect and flags_before and set(flags_before) <= _ENGAGEMENT_FILLER_LABELS:
            # The reply's ONLY sin is a STOCK filler question — it was trying to engage. Deleting
            # it dead-ends the turn and abandons the user (the reported "how are you?" → "Doing
            # well, thanks for asking!" and nothing). Swap the stock question for a genuine/
            # reciprocal one so the companion keeps the conversation alive from ITS side.
            text = await self._repair_flat_filler(prompt, text, allow_disclosure=allow_disc)
            revised = True
            scrubbed_used = bool(find_forbidden(text, allow_disclosure=allow_disc))
        elif self._self_reflect and flags_before:
            # Latency: try the DETERMINISTIC scrub FIRST (no LLM). If it drops the offending
            # assistant-speak while leaving a natural reply, use it and SKIP the ~1s LLM rewrite
            # — with a strong model we rarely need to regenerate, and the scrub is what enforces
            # the rule anyway. Only fall back to the LLM rewrite when scrubbing would gut the
            # reply (e.g. the whole draft was assistant-speak).
            quick = scrub_forbidden(text, allow_disclosure=allow_disc)
            if (
                quick
                and len(quick.split()) >= 3
                and not find_forbidden(quick, allow_disclosure=allow_disc)
            ):
                text = quick
                scrubbed_used = True
                revised = True
            else:
                text = await self._rewrite_assistant_speak(prompt, text, flags_before)
                revised = True
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
        # Brevity/register enforcement (#4): the draft prompt cannot make gemini-flash brief,
        # so compress a too-long / greeting-card reply HERE — after self-reflection, before it is
        # spoken. Fires only when the reply actually reads verbose, so short replies are untouched.
        if self._self_reflect and _reads_verbose(text, prompt.utterance):
            text = await self._rewrite_brief(prompt, text)

        # Enforcement runs HERE, not only in `_finish`, because `_stream_reply` speaks the
        # text this method returns. A reply enforced after it has been spoken is a companion
        # that audibly walks back its own words. `_finish` enforces again as a backstop for
        # any future caller; on an already-clean reply that second pass is a no-op.
        text, caught = self._enforce(prompt, text, allow_disclosure=allow_disc)
        return text, action, caught

    def _span(self, event: str, *, level: str = "info", **fields: Any) -> None:
        """Emit a reasoning span into the current turn's trace (via the logger)."""
        if self._logs is not None:
            self._logs.log(level, event, stage=event, **fields)

    async def _rewrite_instruction(self, text: str) -> str:
        """The self-reflection rewrite instruction + the draft, from prompt
        management if wired (F13), else the bundled default. `.get` is sync + cached,
        so it runs in a thread to keep the reply path unblocked."""
        if self._prompts is not None:
            try:
                rendered = await asyncio.to_thread(
                    self._prompts.get, "self_reflection_rewrite", variables={"draft": text}
                )
                if rendered.text.strip():
                    return rendered.text
            except Exception:
                pass
        return _REWRITE_INSTRUCTIONS + text

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
                purpose="disclosure_polish",
            )
        except PROGRAMMING_ERRORS:
            raise
        except Exception:  # a polish is optional — ANY provider failure keeps the draft
            return text
        candidate = _sanitize_tags(completion.text.strip())
        # Only accept the polish if it STILL discloses honestly — never let the
        # warm rewrite silently drop the required "I'm an AI" (§1.2 rule 4).
        if candidate and _HAS_DISCLOSURE.search(candidate):
            return candidate
        return text

    async def _rewrite_assistant_speak(
        self, prompt: AssembledPrompt, text: str, flags: list[str] | None = None
    ) -> str:
        """Bounded rewrite pass(es) to strip service-desk phrasing; keep the original
        if the rewrite is empty, worse, or the provider is down.

        C2: the instruction now NAMES the shapes the detector found. A generic "remove
        assistant phrasing" left "I'm really sorry about that" and "or something else?"
        in the rewrite, because the model could not see what we objected to. If the first
        rewrite is still dirty, one more attempt runs on the escalated tier; the cleanest
        candidate wins.

        F13: the rewrite instruction is fetched from prompt management (Langfuse)
        when wired, so it's versioned/editable without a code change; falls back to
        the bundled default (== _REWRITE_INSTRUCTIONS) so behaviour is unchanged."""
        found = flags if flags is not None else find_forbidden(text)
        best, best_flags = text, len(found)
        for attempt in range(2):
            instruction = await self._rewrite_instruction(best)
            if found:
                instruction += _critique_note(found)
            try:
                completion = await self._llm.complete(
                    prompt.user_id,
                    [{"role": "user", "content": instruction}],
                    "simple" if attempt == 0 else "moderate",
                    session_id=prompt.session_id,
                    purpose="style_rewrite",
                )
            except PROGRAMMING_ERRORS:
                raise
            except Exception:  # a style rewrite is optional — keep the best draft so far
                return best
            candidate = _sanitize_tags(_strip_fences(completion.text)).strip().strip('"')
            if not candidate or _is_degenerate_rewrite(best, candidate):
                # A rewrite that guts the reply is not "cleaner", it is broken. Observed:
                # an excited turn came back as the single word "Hey," because a one-word
                # answer trivially carries zero forbidden shapes.
                return best
            found = find_forbidden(candidate)
            if len(found) < best_flags:  # strictly cleaner → keep it
                best, best_flags = candidate, len(found)
            if not found:  # fully clean; no second pass needed
                return best
        return best

    async def _rewrite_brief(self, prompt: AssembledPrompt, text: str) -> str:
        """Compress a too-long / greeting-card reply to how a friend actually talks. Keeps the
        original if the rewrite is empty, a single word, or the provider is down (#4)."""
        instructions = _BRIEF_REWRITE_INSTRUCTIONS
        if somber_content(prompt.utterance):
            # The brevity pass otherwise casual-ifies a grave reply into flippant slang
            # ("torched himself, man") — hold the register somber and respectful here too.
            instructions += (
                "IMPORTANT: this is about a death or a tragedy — keep it QUIET, gentle and "
                "respectful. NO flippant slang for the death ('torched himself', 'offed', "
                "'man'/'dude'); keep any [gentle]/[soft]/<pause> delivery tags.\n"
            )
        try:
            completion = await self._llm.complete(
                prompt.user_id,
                [{"role": "user", "content": instructions + text}],
                "simple",
                session_id=prompt.session_id,
                purpose="brevity_rewrite",
            )
        except PROGRAMMING_ERRORS:
            raise
        except Exception:  # a brevity rewrite is optional — keep the original draft
            return text
        candidate = _sanitize_tags(_strip_fences(completion.text)).strip().strip('"')
        if len(candidate.split()) < 2:  # never gut the reply to nothing
            return text
        return candidate

    async def _repair_flat_filler(
        self, prompt: AssembledPrompt, text: str, *, allow_disclosure: bool
    ) -> str:
        """Swap a STOCK filler question for a genuine/reciprocal one, keeping the turn engaging.

        The deterministic `scrub_forbidden` would DELETE the offending sentence — but here the
        whole sentence WAS the engagement (the model just phrased the follow-up as 'what's on your
        mind?'), so deleting it strands the user. We rewrite instead. Falls back to the scrub (a
        flat-but-clean reply) only if the rewrite is empty or still carries the stock phrase, so
        the response standard is never violated even when the rewrite is unhelpful."""

        def _safe() -> str:  # clean, if flat, fallback — never worse than today
            return scrub_forbidden(text, allow_disclosure=allow_disclosure) or text

        try:
            completion = await self._llm.complete(
                prompt.user_id,
                [{"role": "user", "content": _ENGAGEMENT_REWRITE_INSTRUCTIONS + text}],
                "simple",
                session_id=prompt.session_id,
                purpose="engagement_rewrite",
            )
        except PROGRAMMING_ERRORS:
            raise
        except Exception:  # the rewrite is best-effort — never fail the turn on it
            return _safe()
        candidate = _sanitize_tags(_strip_fences(completion.text)).strip().strip('"')
        if len(candidate.split()) < 3 or find_forbidden(
            candidate, allow_disclosure=allow_disclosure
        ):
            return _safe()  # rewrite didn't clear the stock phrase → clean deterministic fallback
        return candidate

    async def _dispatch_tool(
        self,
        prompt: AssembledPrompt,
        dispatcher: "ToolDispatch",
        context: "ToolContext",
        req: ToolRequest,
    ) -> "str | ConfirmRequest":
        """Run one tool per §8; return a note for the model, honest about failures.

        Live-info background tools (web_search) are resolved IN-TURN when they're
        quick (brief §8.8/§8.11) so the model answers now with real data instead of
        promising a result that only arrives later; a genuinely slow search falls
        back to the background/waiter path (§14).
        """
        args = dict(req.args)
        # Clean a web_search query BEFORE we trace or dispatch it, so the search block in the
        # trace shows the query actually run — not the model's raw args with a stale/appended
        # date ("...July 2026") that the handler would strip anyway (user: the UI still showed
        # the date). The handler re-cleans as defense; this just makes the trace honest.
        if req.tool_id == "web_search" and isinstance(args.get("query"), str):
            args["query"] = _strip_unrequested_date(args["query"], prompt.utterance)
        call = ToolCall(tool_id=req.tool_id, args=args)
        # Carry the user's utterance so handlers can validate model-supplied args against
        # what the user actually said (set_companion_name rejecting a name the user never
        # gave — the "Norsylinder" self-naming bug). See core/tools/registry.py.
        context = context.model_copy(update={"utterance": prompt.utterance})
        # Trace the tool CALL with its arguments (e.g. the exact search query) so the
        # turn is fully inspectable — the user reported not seeing what was searched.
        self._span("tool", tool=req.tool_id, phase="request", args=args)
        available = offered_tools(prompt, dispatcher.tools_for(context))
        if not any(t.id == req.tool_id for t in available):
            # D-14: the model asked for a tool this turn does not offer. Do not dispatch it.
            self._span("tool", tool=req.tool_id, phase="result", status="not_offered_this_turn")
            return (
                f"(the tool '{req.tool_id}' is not available on this turn — stay with the "
                "person and answer from what you already know)"
            )
        spec = next((t for t in available if t.id == req.tool_id), None)
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
            self._span("tool", tool=req.tool_id, phase="result", status="queued_background")
            return (
                f"(started '{req.tool_id}' in the background; its result will arrive at a "
                "pause — briefly say you're on it, then continue naturally)"
            )
        # A failed/timed-out tool envelope: tell the model plainly, don't fabricate.
        if isinstance(outcome, ToolResult) and not outcome.ok:
            self._span("tool", tool=req.tool_id, phase="result", status=str(outcome.status))
            return (
                f"(tool '{req.tool_id}' didn't complete ({outcome.status}) — tell the user "
                "plainly that this step failed; do not fabricate a result)"
            )
        output_json = json.dumps(outcome.output)
        # The actual RESULT the tool returned (search summary, project state, …) —
        # recorded so the trace shows what came back, not just that a tool ran.
        self._span("tool", tool=req.tool_id, phase="result", status="ok", result=output_json[:2000])
        return f"(tool '{req.tool_id}' returned: {output_json[:1500]})"

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
        # U8: explicit per-turn delivery register from the emotional read.
        _register, directive = prosody_directive(
            prompt.emotion, somber=somber_content(prompt.utterance)
        )
        instructions += f"\nDelivery register for THIS turn: {directive}"
        instructions += f"\n{_DELIVERY_TAGS_GUIDE}"  # instant + wrapping effects, used properly
        if prompt.emotion:
            instructions += f"\n(Raw emotion signal: {json.dumps(prompt.emotion)})"
        if dispatcher is not None and context is not None:
            # D-14: on an emotionally heavy turn that needs no live info, the external-world
            # tools are not offered at all. A tool the model cannot see is a tool it cannot
            # reach for, which is the only way to stop the agentic loop searching for
            # "grief support resources" at someone who has just lost their father.
            instructions += _render_tool_instructions(
                offered_tools(prompt, dispatcher.tools_for(context)), tool_notes or []
            )
        messages = [
            *prompt.messages[:-1],
            {"role": "system", "content": instructions},
            prompt.messages[-1],
        ]
        # L5 right-size the model to the turn: SIMPLE/MODERATE turns (greetings, casual
        # chat, most replies) run on the FAST tier — a greeting does not need the mature
        # reasoning model, and the ~2s it saves is the single biggest latency win (L0
        # profile). COMPLEX turns — and turns where the user explicitly pinned a
        # "thinking" model — still use the mature reasoning tier for depth (quality
        # holds where it matters, P0). Attempt 1 escalates one tier on failure.
        base: Tier
        main_model: str | None
        if prompt.reasoning_model_override:
            base = self._reasoning_tier  # user explicitly chose a thinking model
            main_model = prompt.reasoning_model_override
        elif prompt.complexity_hint == "complex":
            base = self._reasoning_tier  # hard turns → mature model
            main_model = prompt.model_override
        else:
            base = prompt.complexity_hint  # simple/moderate → the fast tiers
            main_model = prompt.model_override  # the user's fast model if they set one
        # P4: on a SIMPLE turn (greeting / casual), turn OFF the fast model's built-in
        # "thinking" — a one-line social reply needs no chain-of-thought, and Gemini
        # 2.5 Flash thinks by default (extra latency). MODERATE/COMPLEX keep it.
        resp_reasoning = {"enabled": False} if prompt.complexity_hint == "simple" else None
        attempts = [(base, main_model), (_ESCALATE_TIER[base], None)]
        for attempt, (tier, model) in enumerate(attempts):  # rule 1: validate; retry once
            try:
                result = await self._llm.complete(
                    prompt.user_id,
                    messages,
                    tier,
                    response_format={"type": "json_object"},
                    session_id=prompt.session_id,
                    model=model,  # §4 user fast-model choice (first attempt only)
                    reasoning=resp_reasoning,  # P4: no thinking on simple turns
                    temperature=REPLY_TEMPERATURE,  # P2: moderate for warmth
                    max_tokens=REPLY_MAX_TOKENS,  # P1: generous safety ceiling
                    cache_prefix=prompt.cache_prefix,  # L6: cache the stable prefix
                    purpose="response",
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
        _register, directive = prosody_directive(
            prompt.emotion, somber=somber_content(prompt.utterance)
        )
        messages = [
            *prompt.messages[:-1],
            {
                "role": "system",
                "content": f"{_SPOKEN_REPLY_INSTRUCTIONS}\nDelivery register: {directive}",
            },
            prompt.messages[-1],
        ]
        try:
            result = await self._llm.complete(
                prompt.user_id,
                messages,
                _ESCALATE_TIER[self._reasoning_tier],
                session_id=prompt.session_id,
                temperature=REPLY_TEMPERATURE,  # P2: moderate for warmth
                max_tokens=REPLY_MAX_TOKENS,  # P1: generous safety ceiling
                cache_prefix=prompt.cache_prefix,  # L6: cache the stable prefix
                purpose="response_plain",
            )
        except PROGRAMMING_ERRORS:
            raise
        except Exception:  # ANY provider failure → canned safe line (never a raise, D-9)
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
        caught: list[str] | None = None,
    ) -> GenerationResult:
        """The single door out of the engine. `caught` is what `_apply_gates` already found on
        the draft; when it is None this text has not been enforced yet (the `_disambiguate`
        path) and `_enforce` both cleans it and reports what it removed."""
        # A required nature disclosure is not a style violation (§1.2 rule 4).
        allow_disc = bool(judgment and judgment.requires_nature_disclosure)
        text, enforced_here = self._enforce(prompt, text, allow_disclosure=allow_disc)
        caught = list(caught or []) + enforced_here
        # Strip a stale "as of <past year>" stamp the model tacks on from its training cutoff,
        # which makes a freshly-searched answer read as out of date (eval harness caught
        # "…the current PM of Nepal as of 2024"). The real date is already in the prompt.
        text = _strip_stale_datestamp(text)
        # Drop a leading "let me look that up" the model glued onto the ANSWER — the spoken
        # interjection already said it, so hearing it again fused to the fact made the filler
        # and the answer sound like one blob (user report). The answer is just the substance.
        text = _strip_leading_filler(text)

        # ``text`` still carries whitelisted delivery tags (for TTS); the chat UI
        # and stored memory get the tag-free version (brief §1.4).
        # U8: deterministic prosody backstop — never laugh on a down/stressed turn,
        # even if the model slipped a levity tag in. Register is from the emotional
        # read; recorded in the trace as proof prosody was selected per emotion.
        register = effective_register(prompt.emotion, prompt.utterance)
        # Never say a leaked tool token ("web_search:: …") out loud — scrub it from
        # both the spoken and the displayed text (user report; defense-in-depth).
        voice_text = strip_tool_leak(strip_inappropriate_tags(text, register))
        self._span("prosody", register=register, emotion=prompt.emotion or {})
        clean_text = strip_tool_leak(_strip_all_tags(voice_text))
        # Rule 6: every turn logs to the self-model (cost is logged by §11).
        record = TurnRecord(
            user_id=prompt.user_id,
            confidence=judgment.intent_confidence if judgment else 0.2,
            facts_used=[c.entity_id for c in prompt.resolved_entities],
            novel_claim=bool(judgment and judgment.novelty_score > 0.7),
            capability_boundary_flag=judgment.capability_boundary_flag if judgment else None,
        )
        await self._self_model.log(record, statement_text=clean_text)
        escaped = find_forbidden(clean_text, allow_disclosure=allow_disc)
        if escaped:  # `_enforce` should have made this unreachable; never fail silently
            logger.error("ENFORCEMENT ESCAPE: shipping a flagged reply: %s", escaped)
            self._span("enforcement", level="error", rule="escape", flags=escaped)
        return GenerationResult(
            final_text=clean_text,
            voice_text=voice_text,
            action=action,
            judgment=judgment,
            turn_id=record.turn_id,
            style_flags=caught,
        )

    def _enforce(
        self, prompt: AssembledPrompt, text: str, *, allow_disclosure: bool
    ) -> tuple[str, list[str]]:
        """The last gate before the reply leaves the engine (D-7, D-8, D-16).

        `_finish` used to compute `style_flags`, log a warning, and return the reply anyway.
        The detector detected; nothing enforced. A `GenerationResult` carrying `style_flags` is
        by construction a reply the engine itself judged to be assistant-speak, and it was
        being spoken 23% of the time.

        Two rules, both absolute:

        1. **A flagged draft never ships.** Drop the offending sentences. If nothing
           natural survives, say something honest instead of something wrong.
        2. **An acknowledgement never ships.** "I'll grab that for you right away, Nandi!" was
           the entire final spoken reply to a price question. A promise the turn cannot keep is
           worse than an honest miss (§16), so it becomes one.

        This runs on EVERY exit — the streamed path, the agentic path, and each fallback —
        because it lives in `_finish`, which is the single door out of the engine.
        """
        # Rule 2 first: an ack is a whole-reply property, and scrubbing its sentences one by
        # one would leave a fragment rather than reveal the missing answer.
        #
        # It only applies when the turn OWED the user information. "I'll take that as a
        # compliment." is a promise-shaped sentence with no answer in it, and on a social turn
        # it is exactly the right thing to say. What makes a promise a defect is the question
        # it was supposed to answer.
        owes_an_answer = _requires_live_lookup(prompt)
        if owes_an_answer and is_bare_acknowledgement(text, allow_disclosure=allow_disclosure):
            self._span(
                "enforcement", level="warn", rule="acknowledgement_never_final", draft=text[:200]
            )
            return (
                _SEARCH_FAILED_TEXT if prompt.needs_live_info else _NOT_FOUND_TEXT,
                ["bare acknowledgement"],
            )

        # Strip frat-boy slang ('Oh dude, …') surgically before the forbidden-shape gate,
        # so the displayed/text reply matches the professional register (students/pros).
        text = strip_slang(text)
        flags = find_forbidden(text, allow_disclosure=allow_disclosure)
        if not flags:
            return text, []

        scrubbed = scrub_forbidden(text, allow_disclosure=allow_disclosure)
        # A scrub that guts the reply is not a cleaner reply, it is a broken one — the same
        # rule `_is_degenerate_rewrite` applies to the LLM rewrite, applied to the deterministic
        # one. And a scrub that leaves an ack behind ("I'll check that for you.") is no better.
        salvaged = (
            scrubbed
            and not find_forbidden(scrubbed, allow_disclosure=allow_disclosure)
            and not _is_degenerate_rewrite(text, scrubbed)
            and not is_bare_acknowledgement(scrubbed, allow_disclosure=allow_disclosure)
        )
        self._span(
            "enforcement",
            level="warn",
            rule="flagged_draft_never_final",
            flags=flags,
            salvaged=bool(salvaged),
            draft=text[:200],
        )
        return (scrubbed if salvaged else _SAFE_FALLBACK_TEXT), flags


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
        # Additional Grok wrapping tags (volume / vocal-style categories, docs.x.ai
        # audio/text-to-speech) exposed by the voice-effects capability (§10.2).
        "loud",
        "loudly",
        "sing",
        "singing",
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


_STALE_DATESTAMP = re.compile(
    r"[\s,;(—-]*\bas of\b(?:\s+\w+){0,2}\s+((?:19|20)\d\d)\b\)?[,;]?",
    re.IGNORECASE,
)


# ONLY unambiguous lookup fillers — never "okay/sure/hmm", which are legitimate warmth on a
# normal reply. These are the "I'm going to go check" prefaces the interjection already covered.
_LEADING_FILLER = re.compile(
    r"^\s*(?:(?:on it|hang on|one sec(?:ond)?|let me see|let me check|let me look(?: that)?"
    r"(?: up)?|let me dig(?: into that)?|let me find(?: that)?|checking(?: that)?(?: now)?|"
    r"give me a (?:sec|second|moment))[\s,!.…—-]*)+",
    re.IGNORECASE,
)


def _strip_leading_filler(text: str) -> str:
    """Drop a leading lookup-filler the model tacked onto the ANSWER ("Let me look that up. …")
    — the interjection already said that, and hearing it again glued to the fact made them
    sound like one blob (user report). Only touches a LEADING filler and only when one is
    actually present (so a normal reply is returned byte-identical); never returns empty."""
    stripped = _LEADING_FILLER.sub("", text)
    if stripped == text:  # no filler prefix → leave the reply exactly as-is
        return text
    stripped = stripped.lstrip(" ,.!…—-")
    if not stripped.strip():
        return text
    return stripped[0].upper() + stripped[1:]  # re-capitalize the new opening word


def _search_grounded(tool_notes: "list[str]") -> bool:
    """True if a web_search this turn actually RETURNED results (found=true) — not merely
    that a search was attempted. The model can pass reasoning/garbage as its own query,
    which "searched" but found nothing; the answer would then be its stale guess, so a
    volatile turn must re-search cleanly when no search was grounded."""
    for note in tool_notes:
        if (
            "returned:" in note
            and "web_search" in note
            and re.search(r'"found"\s*:\s*true', note, re.IGNORECASE)
        ):
            return True
    return False


def _strip_stale_datestamp(text: str) -> str:
    """Remove an "as of <year>" stamp the model appends from its training cutoff (e.g.
    "…the current PM of Nepal as of 2024") when that year is already PAST — it makes a
    fresh, searched answer read as stale/wrong. A current-or-future year is left alone."""
    this_year = datetime.now(UTC).year

    def _drop(m: re.Match[str]) -> str:
        return "" if int(m.group(1)) < this_year else m.group(0)

    cleaned = re.sub(r"\s{2,}", " ", _STALE_DATESTAMP.sub(_drop, text))
    return cleaned.lstrip(" ,;—-").strip()


_MULTIPART = re.compile(
    r"\b(and also|as well as|along with|and what|and how|and when|and who|and where|and why|"
    r"breakdown|each of|both of|list of|top \d|pros and cons|compare|versus|\bvs\b)\b",
    re.IGNORECASE,
)


def _is_multipart_question(utterance: str) -> bool:
    """Cheap heuristic: does this need SEVERAL distinct facts (→ progressive multi-search), or
    is it ONE fact (→ a single search)? Single-fact is the common, latency-critical case, and
    running the multi-facet planner on it is what made a trivial 'who is the PM' lookup fan out
    into ~13 page extractions. Multi-part = more than one question, or an explicit conjunction/
    comparison/list ask."""
    u = (utterance or "").strip()
    return u.count("?") > 1 or bool(_MULTIPART.search(u))


def _clean_query_list(text: str, *, cap: int = 3) -> list[str]:
    """Parse a model's one-query-per-line reply into clean queries: strip bullets,
    numbering and quotes, drop empties/dupes, cap the count. Used for progressive-search
    planning so an unbounded model reply can never fan out into unbounded searches."""
    out: list[str] = []
    for raw in _strip_fences(text).splitlines():
        line = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", raw).strip().strip('"').strip()
        if line and line.upper() != "ENOUGH" and line.lower() not in {q.lower() for q in out}:
            out.append(line)
        if len(out) >= cap:
            break
    return out


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
    r"|can'?t (provide|give you|tell you|answer)"
    r"|(too|very) ambiguous|need to know what"
    # A hedged GUESS on a volatile fact — the model admits it has no current data, then
    # names a (stale) answer anyway. On a volatile turn this must yield to an honest miss,
    # never ship as fact ("I don't have anything super recent, but Prachanda is PM…").
    r"|don'?t have (?:anything|much|the)?\s*(?:super |very )?"
    r"(?:recent|current|up[- ]?to[- ]?date|latest)"
    r"|not (?:totally |entirely |100% )?(?:sure|certain) (?:who|what|of the current)"
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


# A rewrite must not gut the reply. Anything under this fraction of the original's words
# (and shorter than a natural spoken sentence) is a degenerate answer, not a cleaner one.
_MIN_REWRITE_WORD_RATIO = 0.4
_MIN_REWRITE_WORDS = 4


def _is_degenerate_rewrite(original: str, candidate: str) -> bool:
    orig_words = len(original.split())
    cand_words = len(candidate.split())
    if orig_words <= _MIN_REWRITE_WORDS:
        return False  # the original was already terse; nothing to gut
    return cand_words < _MIN_REWRITE_WORDS or cand_words < _MIN_REWRITE_WORD_RATIO * orig_words


def _strip_query_echo(text: str, query: str) -> str:
    """Remove a search query the model read ALOUD, without touching the answer (D-18).

    The model sometimes dictates its own query before answering:

        "I'll check that right now. OP NEPSE LTP current price  The current LTP of OP is 308.90."
                                    └────────── the echo ─────┘

    This used to delete the query string wherever it occurred. `_build_search_query` produces
    ordinary noun phrases, and an ordinary noun phrase is exactly what a correct answer
    contains, so asking "who is the current prime minister of Nepal?" produced:

        query : "current prime minister of Nepal"
        reply : "The is Balendra Shah! He's also the youngest person to ever hold that…"

    The engine searched, found the right answer, and mutilated it on the way out.

    An echo is a **standalone fragment**: it starts the reply or follows a sentence end, and
    is followed by the start of a new sentence or the end of the text. The same words flowing
    through a sentence — preceded by "The", followed by "is" — are the answer, and are left
    exactly where they are.
    """
    q = query.strip()
    if not q:
        return text
    # (start-of-text | end-of-sentence) QUERY (start-of-sentence | end-of-text)
    echo = re.compile(
        rf"(?:(?<=^)|(?<=[.!?…])\s+){re.escape(q)}\s*(?=[A-Z(\"']|$)",
        re.IGNORECASE,
    )
    cleaned, count = echo.subn(" ", text)
    if not count:
        return text
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return re.sub(r"\s+([.,!?])", r"\1", cleaned)


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
    r"|stock price|share price|exchange rate|price of"
    r"|current time|what(?:'s| is)? the time|time (?:in|right now)"
    r"|today'?s date|what(?:'s| is)? the date|date (?:today|in)"
    r"|what'?s happening (?:in|with|right now|today)|trending"
    # Explicit lookup / research requests (the user is asking me to go find something):
    r"|look (?:it |that |this )?up|look into|search (?:for|up|the)|google"
    r"|find out|dig up|get me (?:some )?(?:detail|details|info|information|the latest)"
    r"|any (?:news|updates?|info|details?|word) (?:on|about)"
    r"|what(?:'s| is| are)? (?:the )?(?:latest|situation|status|update)s? (?:on|with|about|in)"
    r"|(?:did you|have you) hear(?:d)? about|i (?:heard|found out|read) about|tell me about"
    # Current-event / breaking-news nouns that need fresh external info:
    r"|missing|crash(?:ed|ing)?|earthquake|wildfire|outage|explosion|attack|shooting"
    r"|election|died|passed away|breaking|happening|going on)\b",
    re.IGNORECASE,
)


def _is_live_info_query(utterance: str) -> bool:
    return bool(_LIVE_INFO_QUERY.search(utterance))


def _requires_live_lookup(prompt: AssembledPrompt) -> bool:
    """Does this turn need CURRENT real-world info to answer without going stale? (S1)

    The REASONING step decides (`prompt.needs_live_info`, produced by the context/intent
    node, which sees the user's local date and is told that role-holders, prices, scores
    and "still/current/latest" are always volatile). The phrasing regex is kept only as a
    cheap OR-backstop for when the classifier gave no usable answer (`None`) — it is never
    the sole gate again.

    It used to be: `_is_live_info_query(utterance)` and nothing else. That returns False
    for "who is the current prime minister of Nepal?", so the turn took the non-agentic
    streaming path, could never reach a tool, and answered from training data.

    **D-14.** The OR-backstop then bit from the other side. `_LIVE_INFO_QUERY` lists the
    breaking-news noun `died`, so *"my dad died last week and I can't stop crying"* was a
    live-info query — and the engine searched for "grief support resources for losing a
    father", 9 runs out of 10, then read the helplines out. The classifier itself was right
    every single time it ran: `needs_live_info=False`, 5 of 5. The regex overrode it, under a
    comment reading "bias toward searching: a needless search costs a second". A needless
    search costs considerably more than a second when someone has just told you their father
    died.

    Before D-2 the classifier was skipped on simple turns, so `False` was indistinguishable
    from "never asked" and could not be trusted on its own. It now runs on every turn, and an
    explicit `False` on an emotionally heavy turn is a verdict. Presence over facts
    (§3.6.5, §6): a grieving person did not ask for a helpline.
    """
    if prompt.needs_live_info is True:
        return True
    if prompt.needs_live_info is False and _is_emotionally_heavy(prompt):
        return False
    # The classifier's JUDGEMENT is good but its DELIVERY is not: the provider intermittently
    # returns unusable JSON, which used to be swallowed into "don't search". So an unknown
    # verdict is never trusted alone — the deterministic question-shape backstop and the topic
    # regex both get a vote. On a turn carrying no emotional weight, a needless search really
    # does only cost a second, and a stale answer costs the user's trust.
    return is_volatile_question(prompt.utterance) or _is_live_info_query(prompt.utterance)


def _searched_web(seen_calls: set[str]) -> bool:
    """Did a REAL web search run this turn? Only a web search discharges a volatility-flagged
    turn (S1): the model reaching for `search_memory` used to satisfy `not seen_calls` and
    suppress the live lookup, so "what's the price of SYPNL?" answered from a number it had
    stored on an earlier turn — a stale figure, spoken as current."""
    return any(key.startswith("web_search") for key in seen_calls)


def _is_emotionally_heavy(prompt: AssembledPrompt) -> bool:
    """Is the person in front of us grieving, frightened, or in pain? (D-14)

    Read from the reasoning step's `emotional_read`, which `_augment()` turns into
    `prompt.emotion`. Since D-2 that read exists on both callers, so this is not
    caller-dependent.
    """
    return read_register(prompt.emotion) in ("down", "stressed")


# Tools that go out into the world. A grieving user's OWN data (memory, their portfolio) is
# fine to read — reaching for the open web on their behalf is what §6 forbids. This is the
# design's own grouping (§8.5, "External world"), not a new taxonomy.
_EXTERNAL_WORLD_TOOLS = frozenset({"web_search", "fetch_url", "get_realtime_data"})


def offered_tools(prompt: AssembledPrompt, tools: list[ToolSpec]) -> list[ToolSpec]:
    """The tools the model may request this turn (D-14).

    Suppressing the search in `_requires_live_lookup` alone is not enough: the agentic loop
    lets the model request `web_search` itself, and on the grief turn it did. A tool the model
    is never shown is a tool it cannot reach for. Its own data stays available.
    """
    if not _is_emotionally_heavy(prompt) or _requires_live_lookup(prompt):
        return tools
    return [t for t in tools if t.id not in _EXTERNAL_WORLD_TOOLS]


_REPAIR_INSTRUCTIONS = (
    "You just searched the live web for the user's question. Using ONLY these "
    "fresh search results, answer them directly, warmly, and briefly in your own "
    "voice (one or two spoken sentences) — give the ACTUAL answer. If they asked for a "
    "set number of items (e.g. 'top 2 news'), give exactly that many, each a distinct "
    "item.\n"
    "If they asked whether they should DO something — 'should I bring an umbrella?', 'do I "
    "need a jacket?', 'is it worth going?' — LEAD with your recommendation the way a friend "
    "would ('yeah, grab one — looks like rain this afternoon'), NOT a forecast or data "
    "readout. The facts are your reason in a few words, never the whole reply.\n"
    "If the honest answer is a long list or several items each needing a paragraph, DON'T "
    "read the whole thing aloud — give a short spoken summary of just the headline points "
    "(two or three, in a sentence or two) and invite them to dig into any one. When they "
    "then ask about a specific one, go deep on THAT one only.\n"
    "If their message carried FEELING (pain, worry, grief, excitement), meet that FIRST "
    "in one short human sentence and stay with them — then give the facts briefly. Never "
    "reel off a list of news items at someone who just told you they're hurting.\n"
    "If the results genuinely don't contain the answer, say so plainly in one friendly "
    "line ('I had a look and couldn't find anything current on that') — never a formal "
    "refusal, never 'I can't provide', never 'your query is ambiguous', and never ask "
    "them to rephrase.\n"
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
