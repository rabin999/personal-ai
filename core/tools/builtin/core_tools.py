"""Core (non-project) tools — the MVP tool set (design doc §8.5).

Registered against the shared registry at startup so the agentic loop (§14.11)
can actually DO things: recall the user's memory, look up live info on the web
(background), read semantic facts, and adjust audio sensitivity. Project tools
(e.g. finance ``log_entry``) are registered by §16 per instance.

Handlers are ``(args, ToolContext) -> dict`` and take ``user_id`` from the
resolved context (never hard-coded). ``web_search`` is a *background* tool: the
dispatcher enqueues it (§8.6 — never run search inline) and the worker executes
this handler, so its result comes back at a pause (§14).
"""

import logging
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from core.memory.episodic import EpisodicMemory
from core.memory.semantic import SemanticMemory
from core.profile import ProfileService
from core.projects.service import ProjectService
from core.tools.registry import ToolContext, ToolRegistry, ToolSpec
from core.tools.results import ToolResultStore
from core.tools.web_search import WebSearch
from ports.retrieval import RetrievalPort, VerifiedRetrievalError

logger = logging.getLogger(__name__)


def _norm_tokens(text: str) -> set[str]:
    """Accent-folded, lowercase word tokens — for checking a name against an utterance."""
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return {t for t in re.findall(r"[a-z0-9]+", folded.lower()) if len(t) >= 2}


def _strip_unrequested_stale_year(query: str, utterance: str) -> str:
    """Remove a PAST year the model bolted onto a live-search query that the user never
    said. Models anchor to their training cutoff and append e.g. '...2024' to a 'current'
    question, which pins the search to a stale year and returns nothing usable (the
    honest-fail we saw on 'who is the current PM of Nepal?'). We only strip a year that
    is (a) not in the user's own words and (b) before this year — a year the user asked
    about ('who won in 2019') stays untouched. Design doc §15 / RETRIEVAL_POLICY.md."""
    this_year = datetime.now(UTC).year
    said = set(re.findall(r"\d{4}", utterance))

    def _drop(m: re.Match[str]) -> str:
        year = m.group(0)
        if year in said or int(year) >= this_year:
            return year
        return ""

    cleaned = re.sub(r"\b\d{4}\b", _drop, query)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _name_came_from_user(name: str, utterance: str) -> bool:
    """True if the user actually said this name — every alphabetic token of the name
    appears in their utterance. Blocks the companion from self-naming with a word the
    user never uttered (the "Norsylinder" bug). Lenient when the utterance is unknown
    (empty), so non-conversational callers are unaffected."""
    if not utterance.strip():
        return True
    name_tokens = _norm_tokens(name)
    if not name_tokens:
        return False
    return name_tokens <= _norm_tokens(utterance)


# VAD nudge per "you're too sensitive" style request (§11.3); clamped in §2.
_VAD_STEP = 0.12

FINANCE_TYPE = "finance_portfolio"


def register_core_tools(
    registry: ToolRegistry,
    *,
    episodic: EpisodicMemory,
    semantic: SemanticMemory,
    web_search: WebSearch,
    profiles: ProfileService,
    projects: ProjectService,
    results: ToolResultStore | None = None,
    retrieval_builder: Callable[[str, str | None], RetrievalPort] | None = None,
) -> None:
    async def search_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        hits = await episodic.retrieve(ctx.user_id, str(args.get("query", "")), k=5)
        return {"snippets": [h.text for h in hits]}

    registry.register(
        ToolSpec(
            id="search_memory",
            description="Search this user's past conversations (episodic memory) for "
            'relevant snippets. args: {"query": str}',
            type="readonly",
            latency_class="fast",
        ),
        search_memory,
    )

    async def get_semantic_facts(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        facts = await semantic.profile_facts(ctx.user_id, limit=10)
        return {"facts": [f.fact for f in facts]}

    registry.register(
        ToolSpec(
            id="get_semantic_facts",
            description="Get stable known facts about this user (name, people, "
            "preferences). args: {}",
            type="readonly",
            latency_class="fast",
        ),
        get_semantic_facts,
    )

    async def web_search_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        query = _strip_unrequested_stale_year(str(args.get("query", "")), ctx.utterance)
        # §15 verified retrieval: read the pages + cross-check rather than trust a snippet.
        # Degrade to the snippet search on a crawler/dependency failure so live info never
        # regresses to nothing; our OWN bugs (VerifiedRetrievalError) still fail loud (D-9).
        if retrieval_builder is not None:
            try:
                verified = await retrieval_builder(ctx.user_id, ctx.session_id).verify(query)
                if verified.status != "error":
                    return {
                        "summary": verified.formatted_voice,
                        "sources": [s.url for s in verified.sources][:5],
                        "found": verified.status in ("corroborated", "single_source"),
                    }
            except VerifiedRetrievalError:
                raise
            except Exception:
                logger.warning(
                    "verified retrieval failed; falling back to snippet search", exc_info=True
                )
        outcome = await web_search.run(query, ctx.user_id, ctx.session_id)
        return {
            "summary": outcome.summary,
            "sources": [s.url for s in outcome.sources][:5],
            "found": bool(outcome.sources),
        }

    registry.register(
        ToolSpec(
            id="web_search",
            description=(
                "Search the LIVE web for current / real-time info — news, sports scores, "
                "prices, events, 'what's happening'. Write a SELF-CONTAINED, specific "
                "query with enough context to get the RIGHT result: include the concrete "
                "subject + any key entities/place from the conversation (resolve pronouns "
                "into the concrete subject from the conversation), and add "
                "'latest' or 'today' for unfolding events. Don't send a vague fragment. "
                "For a MULTI-PART question (an event PLUS its cause PLUS a related rule/number), "
                "do ONE focused search per distinct fact and call this again for each — then "
                "answer once you have enough; don't cram several facets into one vague query. "
                'args: {"query": str}'
            ),
            type="background",
            latency_class="slow",
            # Measured: a cold search is ~6 s (Serper ~3 s + the summarize LLM ~3 s), and
            # "right now"/"today" queries deliberately bypass the cache, so they are ALWAYS
            # cold. The dispatcher's flat 8 s inline default timed them out at 8002 ms —
            # "what's the weather in Kathmandu right now?" returned nothing. It overlaps the
            # spoken ack, so a longer budget costs the user no extra silence.
            inline_timeout_s=20.0,
        ),
        web_search_tool,
    )

    async def set_companion_name(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        name = str(args.get("name", "")).strip()[:40]
        if not name:
            return {"error": "no name given"}
        # Only accept a name the user actually said — never one the model coined itself.
        # This is the fix for the companion self-naming with a hallucinated word
        # ("Norsylinder") the user never uttered. The companion doesn't name itself.
        if not _name_came_from_user(name, ctx.utterance):
            logger.info("rejected self-assigned companion name %r (not in utterance)", name)
            return {
                "error": "not_user_given",
                "note": (
                    "Don't name yourself. Only call this when the user gives you a name — "
                    "use the exact name they said. If they haven't, ask what they'd like to "
                    "call you instead of inventing one."
                ),
            }
        # The profile is the SINGLE source of truth for the companion's name (read back
        # in prompt assembly). We deliberately do NOT also write a semantic episode: doing
        # so let the extractor resolve "the companion is called X" into a phantom PERSON
        # entity that fragmented the user's own identity in the graph (the Norsylinder /
        # Cylinder / Marshal tangle). Companion identity lives in the profile, not the
        # user's memory graph (design doc §3.1 memory correctness).
        await profiles.update(ctx.user_id, {"companion_name": name, "onboarded": True})
        return {"companion_name": name}

    registry.register(
        ToolSpec(
            id="set_companion_name",
            description="Remember the name the USER gives you (the companion). Call this "
            "ONLY when the user actually tells you what to call you, using their exact "
            "word. Never invent or pick a name yourself. If they haven't given one, ask — "
            'don\'t make one up. args: {"name": str}',
            type="action",
            latency_class="fast",
            requires_confirmation=False,
        ),
        set_companion_name,
    )

    async def set_user_name(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        name = str(args.get("name", "")).strip()[:40]
        if not name:
            return {"error": "no name given"}
        # Same guard as the companion name: only accept a name the user actually said,
        # so the model can't invent or mis-hear one. This is the canonical place the
        # user's name lives — updating it here is how a name change supersedes the old
        # one everywhere at once, instead of leaving stale name-entities in the graph
        # that make the companion "call me by the old name" (design doc §3.1).
        if not _name_came_from_user(name, ctx.utterance):
            logger.info("rejected user name %r (not in utterance)", name)
            return {
                "error": "not_user_given",
                "note": (
                    "Only set the user's name to what they actually told you, using their "
                    "exact word. If you're unsure what they're called, ask — don't guess."
                ),
            }
        await profiles.update(ctx.user_id, {"user_name": name})
        return {"user_name": name}

    registry.register(
        ToolSpec(
            id="set_user_name",
            description="Remember the name the USER goes by (what to call THEM). Call this "
            "when they tell you their name, or correct it, using their exact word. This is "
            "the canonical name you address them by, so setting it replaces any older name. "
            'Never guess — if unsure, ask. args: {"name": str}',
            type="action",
            latency_class="fast",
            requires_confirmation=False,
        ),
        set_user_name,
    )

    # NOTE: trade persistence is NOT a conversational tool. Memory writes (episodic
    # events, semantic facts, and the trade ledger) all go through the single explicit
    # extraction step (core/memory/extraction.py, brief §1) — so the chat model can't
    # double-write a trade by also calling a tool mid-turn. See REMEDIATION_LOG.

    if results is not None:

        async def recall_tool_result(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            # §5.2: "what was that news / the last search result?" — resolve against
            # the stored tool results for THIS user, never a hallucination.
            tool = str(args.get("tool") or "").strip() or None
            stored = await results.latest(ctx.user_id, tool=tool, limit=3)
            return {
                "results": [
                    {"tool": d.get("tool"), "query": d.get("query"), "output": d.get("output")}
                    for d in stored
                ]
            }

        registry.register(
            ToolSpec(
                id="recall_tool_result",
                description="Recall the result of a recent tool call the user is asking "
                "back about ('what was that news?', 'what did that search find?', 'the "
                'last result you looked up\'). args: {"tool": str (optional, e.g. '
                '"web_search")}',
                type="readonly",
                latency_class="fast",
            ),
            recall_tool_result,
        )

    async def update_audio_prefs(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        profile = await profiles.get(ctx.user_id)
        threshold = profile.audio_prefs.vad_threshold
        direction = str(args.get("direction", "")).lower()
        if "less" in direction or "up" in direction or args.get("more_sensitive") is False:
            threshold += _VAD_STEP  # less twitchy about background noise
        elif "more" in direction or "down" in direction:
            threshold -= _VAD_STEP
        updated = await profiles.update(ctx.user_id, {"audio_prefs": {"vad_threshold": threshold}})
        return {"vad_threshold": updated.audio_prefs.vad_threshold}

    registry.register(
        ToolSpec(
            id="update_audio_prefs",
            description="Adjust mic sensitivity when the user says you're picking up too "
            "much/little (clamped to a safe range, persisted). "
            'args: {"direction": "less_sensitive"|"more_sensitive"}',
            type="action",
            latency_class="fast",
            requires_confirmation=False,
        ),
        update_audio_prefs,
    )
