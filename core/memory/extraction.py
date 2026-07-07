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
from ports.llm import LLM, LLMUnavailable
from ports.preference_memory import PreferenceMemory

logger = logging.getLogger(__name__)

FINANCE_TYPE = "finance_portfolio"

_EXTRACT_INSTRUCTIONS = """
You are the MEMORY step of a personal companion. Given the latest exchange,
decide what is worth remembering long-term. Extract ONLY what the USER actually
stated — never invent, never store the companion's own chatter or small talk.

Respond with ONLY a JSON object of this exact shape:
{"episodic_events": ["a specific thing that happened, with its implicit time,
    one short factual sentence"],
 "semantic_facts": ["a durable fact / preference / routine about the user,
    TIME-STRIPPED and generalized (e.g. 'takes blood-pressure medication daily
    around 8pm')"],
 "trades": [{"ticker": "SYMBOL", "side": "buy" or "sell", "qty": number,
    "price": number}],
 "store_nothing": true if this was just small talk / nothing memorable}

Rules:
- Extract ONLY NEW information the USER just stated. If the USER is asking a
  QUESTION, or the COMPANION is merely recalling/confirming something already
  known ("you bought 10 SYPNL", "you take your meds at 8pm"), that is NOT new —
  set store_nothing: true. Never re-store what is only being recalled.
- A concrete NEW event -> an episodic_event AND, if it implies a standing
  fact/routine, a distilled semantic_fact.
- Preferences, relationships, routines, health facts, goals (newly shared) ->
  semantic_facts (generalized, no "today").
- An explicit NEW stock/share buy or sell the user just made -> a trades entry.
- Greetings, thanks, chit-chat, questions, recall/confirmation ->
  store_nothing: true, empty lists.
- Keep every string short and literal; never paraphrase the user into something
  they didn't say.
""".strip()


class ExtractedTrade(BaseModel):
    ticker: str
    side: Literal["buy", "sell"]
    qty: float
    price: float


class Extraction(BaseModel):
    episodic_events: list[str] = []
    semantic_facts: list[str] = []
    trades: list[ExtractedTrade] = []
    store_nothing: bool = False


class ExtractionResult(BaseModel):
    """What actually got written (for the trace / debugging)."""

    episodic_written: int = 0
    semantic_written: int = 0
    trades_written: int = 0
    facts: list[str] = []
    events: list[str] = []


class MemoryExtractor:
    def __init__(
        self,
        llm: LLM,
        episodic: EpisodicMemory,
        semantic: SemanticMemory,
        projects: ProjectService,
        preferences: PreferenceMemory | None = None,
    ) -> None:
        self._llm = llm
        self._episodic = episodic
        self._semantic = semantic
        self._projects = projects
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
                )
                return Extraction.model_validate_json(_strip_fences(result.text))
            except (LLMUnavailable, ValidationError, ValueError, json.JSONDecodeError):
                continue
        logger.warning("memory extraction failed twice; storing nothing this turn")
        return None

    async def _store(
        self, user_id: str, session_id: str, extraction: Extraction
    ) -> ExtractionResult:
        result = ExtractionResult(
            facts=extraction.semantic_facts, events=extraction.episodic_events
        )
        for event in extraction.episodic_events:
            try:
                await self._episodic.write(user_id, session_id, [event])
                result.episodic_written += 1
            except Exception:
                logger.exception("episodic write failed")
        for fact in extraction.semantic_facts:
            try:
                # Graphiti orphans an edge (and then can't retrieve the fact) when
                # the episode has no explicit subject to attach to — verified: a
                # bare "takes meds at 8pm" returns nothing, "The user takes…"
                # returns reliably. So ensure every fact names the user.
                await self._semantic.add_episode(user_id, _with_subject(fact))
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


_SUBJECT_STARTS = ("the user", "user ", "user'", "they ", "he ", "she ")


def _with_subject(fact: str) -> str:
    """Ensure a semantic fact names the user so Graphiti attaches (and can
    retrieve) it. Facts that already start with a user subject are left as-is."""
    stripped = fact.strip()
    if stripped.lower().startswith(_SUBJECT_STARTS):
        return stripped
    return f"The user {stripped[0].lower()}{stripped[1:]}" if stripped else stripped


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0]
    return stripped.strip()
