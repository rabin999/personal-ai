"""Memory extraction & routing — the WRITE step of the read/reason/write loop (brief §1).

Storing every turn blindly is wrong. After the companion responds, this runs an
explicit LLM-driven extraction (Pydantic-validated) that decides what, if
anything, is worth remembering long-term and to WHICH store:

- **episodic** (Qdrant): specific, time-bound events the user reported
  ("took my BP pill at 8pm today", "bought 10 SYPNL at 230").
- **semantic** (Graphiti, temporal validity): durable, time-stripped facts,
  preferences, and routines distilled from those events ("takes blood-pressure
  medication daily around 8pm") — this is what answers a question next session.
- **ledger** (Projects §16): structured records like stock trades.

The raw CONVERSATION LOG is stored separately and always (`ConversationStore`);
this step is the *selective* long-term memory, so we consolidate rather than hoard.

Runs off the latency path (the caller awaits it in the background). Every write is
``user_id``-scoped (§0.5). On validation failure: retry once, then store nothing
(never fabricate memory). Design override logged in docs/REMEDIATION_LOG.md.
"""

import json
import logging
from typing import Literal

from pydantic import BaseModel, ValidationError

from core.memory.episodic import EpisodicMemory
from core.memory.semantic import SemanticMemory
from core.projects.service import ProjectService
from core.psych.persona import STYLE_DIMENSIONS, PersonaStore, StyleSignal
from ports.llm import LLM, LLMUnavailable
from ports.preference_memory import PreferenceMemory

logger = logging.getLogger(__name__)

FINANCE_TYPE = "finance_portfolio"

_DIMS = ", ".join(STYLE_DIMENSIONS)

_EXTRACT_INSTRUCTIONS = f"""
You are the MEMORY step of a personal companion. Given the latest exchange,
decide what — if anything — is worth remembering long-term, and WHERE it belongs.
There are THREE distinct layers; keep them separate (mixing them is a bug):
  • FACTS about the user (what is TRUE) → semantic_facts. Durable, objective.
  • EVENTS the user experienced (what HAPPENED) → episodic_events. Timestamped.
  • PERSONA (HOW they like to be talked to / who they are as a conversational
    partner) → style_signals. Delivery/style, NOT facts.

QUALITY BAR — store MEANINGFUL things only. A few good memories beat many junk
ones. DISCARD and set store_nothing when the turn is: a greeting, thanks, filler,
one-off trivia that won't matter later, the companion's own chatter, a question,
a recall/confirmation of something already known, or a malformed/garbled fragment.
When in doubt, store LESS.

Respond with ONLY a JSON object of this exact shape:
{{"episodic_events": ["a specific thing that happened, with its implicit time,
    one short factual sentence"],
 "semantic_facts": ["a durable fact / preference / routine about the user,
    TIME-STRIPPED and generalized (e.g. 'takes blood-pressure medication daily
    around 8pm')"],
 "style_signals": [{{"text": "readable 2nd-person statement about how to talk with
    them, e.g. 'You like me to get to the point'", "kind": "style"|"interest"|
    "sensitivity", "dimension": one of [{_DIMS}] or null, "stated": true if the
    user DIRECTLY asked for this ('keep it short'), false if merely inferred}}],
 "trades": [{{"ticker": "SYMBOL", "side": "buy" or "sell", "qty": number,
    "price": number}}],
 "store_nothing": true if this was just small talk / nothing memorable}}

Rules:
- Extract ONLY NEW information the USER just stated. If the USER is asking a
  QUESTION, or the COMPANION is merely recalling/confirming something already
  known ("you bought 10 SYPNL", "you take your meds at 8pm"), that is NOT new —
  set store_nothing: true. Never re-store what is only being recalled.
- CRITICAL — durable vs. transient. A semantic_fact is a STABLE, lasting truth
  about the person: an identity fact, a preference, a routine, a relationship, a
  standing health condition or goal ("takes blood-pressure medication daily around
  8pm", "works at Xenon", "prefers directness", "has a younger sister named Mira").
  A TRANSIENT state — how they feel or what's happening RIGHT NOW / today / this
  moment ("has a headache right now", "is tired today", "was up late last night",
  "is stressed about a deadline") — is NOT a durable fact. Put transient states in
  episodic_events ONLY; NEVER as a semantic_fact. Do not turn "I have a headache
  right now" into a permanent fact about the user.
- PERSONA vs. FACT — style must NOT be stored as a fact. "prefers short, blunt
  answers", "enjoys a bit of humor", "money topics tend to stress them so keep it
  calm", "loves football", "communicates directly" → style_signals (with the right
  kind + dimension), NOT semantic_facts. A style_signal captures DELIVERY (how to
  say things); a fact captures CONTENT (what is true). Emit a style_signal when the
  user asks you to change HOW you talk, reveals a topic interest/dislike, or shows a
  clear, repeated conversational preference — not for a one-off mood.
- Only what the USER stated about THEMSELVES becomes a fact/signal. Never store the
  companion's own suggestions or chatter (e.g. if the companion suggests "rest in a
  dark room", that is NOT a fact about the user).
- A concrete NEW event -> an episodic_event AND, if it implies a standing
  fact/routine, a distilled semantic_fact.
- An explicit NEW stock/share buy or sell the user just made -> a trades entry.
- Keep every string short and literal; never paraphrase the user into something
  they didn't say.
""".strip()

# Deterministic backstop (brief §8.13): even with the prompt above, a weak model
# occasionally files a transient state as a durable fact. A "fact" that is clearly
# about the current moment and carries no durability marker is demoted to episodic
# only — it must never enter semantic memory as a permanent truth.
_TRANSIENT_MARKERS = (
    "right now",
    "at the moment",
    "today",
    "tonight",
    "this morning",
    "this afternoon",
    "this evening",
    "currently",
    "at present",
    "just now",
    "last night",
)
_DURABLE_MARKERS = (
    "daily",
    "every",
    "always",
    "usually",
    "each ",
    "prefers",
    "works at",
    "lives in",
    "name is",
    "named",
    "routine",
    "allergic",
    "diagnosed",
    "birthday",
)


def _looks_transient(fact: str) -> bool:
    """True if a 'fact' describes the current moment with no durability marker."""
    lowered = fact.lower()
    if any(marker in lowered for marker in _DURABLE_MARKERS):
        return False
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


class ExtractedTrade(BaseModel):
    ticker: str
    side: Literal["buy", "sell"]
    qty: float
    price: float


class Extraction(BaseModel):
    episodic_events: list[str] = []
    semantic_facts: list[str] = []
    style_signals: list[StyleSignal] = []
    trades: list[ExtractedTrade] = []
    store_nothing: bool = False


class ExtractionResult(BaseModel):
    """What actually got written (for the trace / debugging)."""

    episodic_written: int = 0
    semantic_written: int = 0
    persona_written: int = 0
    trades_written: int = 0
    facts: list[str] = []
    events: list[str] = []
    persona: list[str] = []


class MemoryExtractor:
    def __init__(
        self,
        llm: LLM,
        episodic: EpisodicMemory,
        semantic: SemanticMemory,
        projects: ProjectService,
        persona: PersonaStore | None = None,
        preferences: PreferenceMemory | None = None,
    ) -> None:
        self._llm = llm
        self._episodic = episodic
        self._semantic = semantic
        self._projects = projects
        self._persona = persona
        self._preferences = preferences

    async def extract_and_store(
        self, user_id: str, session_id: str, user_text: str, assistant_text: str
    ) -> ExtractionResult:
        """Decide what to persist from this exchange, then write it. Best-effort."""
        # Mem0 preference layer (§2): let it extract + store preferences from the
        # raw exchange (its own extraction; best-effort, never blocks).
        if self._preferences is not None:
            await self._preferences.add(
                user_id,
                [
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": assistant_text},
                ],
            )
        extraction = await self._extract(user_id, session_id, user_text, assistant_text)
        if extraction is None or extraction.store_nothing:
            return ExtractionResult()
        return await self._store(user_id, session_id, extraction)

    async def _extract(
        self, user_id: str, session_id: str, user_text: str, assistant_text: str
    ) -> Extraction | None:
        messages = [
            {"role": "system", "content": _EXTRACT_INSTRUCTIONS},
            {
                "role": "user",
                "content": f"USER said: {user_text}\nCOMPANION replied: {assistant_text}",
            },
        ]
        for _ in range(2):  # validate; retry once (§0.5)
            try:
                result = await self._llm.complete(
                    user_id,
                    messages,
                    "simple",
                    response_format={"type": "json_object"},
                    session_id=session_id,
                    temperature=0.2,  # P2: extraction is a decision → low temp
                    purpose="memory_extraction",
                )
                return Extraction.model_validate_json(_strip_fences(result.text))
            except (LLMUnavailable, ValidationError, ValueError, json.JSONDecodeError):
                continue
        logger.warning("memory extraction failed twice; storing nothing this turn")
        return None

    async def _store(
        self, user_id: str, session_id: str, extraction: Extraction
    ) -> ExtractionResult:
        # Deterministic guard (brief §8.13): demote any transient "fact" to an
        # episodic event so a current state never becomes a permanent semantic truth.
        durable_facts: list[str] = []
        events = list(extraction.episodic_events)
        for fact in extraction.semantic_facts:
            if _looks_transient(fact):
                logger.info("demoting transient 'fact' to episodic: %s", fact)
                events.append(fact)
            else:
                durable_facts.append(fact)

        result = ExtractionResult(
            facts=durable_facts, events=events, persona=[s.text for s in extraction.style_signals]
        )
        # PERSONA (brief U2): route style signals to the dynamic persona store — the
        # "How I've learned to talk with you" layer. Kept OUT of semantic facts so
        # style never masquerades as a fact (brief U0). Best-effort, user-scoped.
        if self._persona is not None and extraction.style_signals:
            try:
                result.persona_written = await self._persona.apply(
                    user_id, extraction.style_signals
                )
            except Exception:
                logger.exception("persona write failed")
        for event in events:
            try:
                await self._episodic.write(user_id, session_id, [event])
                result.episodic_written += 1
            except Exception:
                logger.exception("episodic write failed")
        for fact in durable_facts:
            try:
                # record_fact ensures the fact names the user so Graphiti attaches
                # (and can retrieve) it — the fix for empty semantic retrieval.
                await self._semantic.record_fact(user_id, fact)
                result.semantic_written += 1
            except Exception:
                logger.exception("semantic write failed")
        for trade in extraction.trades:
            try:
                project = await self._projects.find_or_create(user_id, FINANCE_TYPE, "My portfolio")
                if await self._is_duplicate_trade(project.id, user_id, trade):
                    logger.info("skipping duplicate trade write: %s", trade.model_dump())
                    continue
                await self._projects.log_entry(project.id, user_id, trade.model_dump())
                result.trades_written += 1
            except Exception:
                logger.exception("ledger write failed")
        return result

    async def _is_duplicate_trade(
        self, project_id: str, user_id: str, trade: ExtractedTrade
    ) -> bool:
        """Guard against re-logging the SAME trade (e.g. when it's being recalled).
        Deterministic backstop behind the prompt's 'don't re-store recall' rule."""
        try:
            state = await self._projects.state(project_id, user_id)
        except Exception:
            return False
        want = trade.model_dump()
        for entry in state.recent_entries:
            data = entry.data
            if (
                str(data.get("ticker", "")).upper() == want["ticker"].upper()
                and str(data.get("side", "")).lower() == want["side"]
                and float(data.get("qty", 0)) == want["qty"]
                and float(data.get("price", 0)) == want["price"]
            ):
                return True
        return False


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0]
    return stripped.strip()
