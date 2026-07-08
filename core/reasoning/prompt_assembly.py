"""Prompt Assembly (spec §10): utterance → final LLM prompt, or a disambiguation halt.

Runs the ordered context pipeline — entity resolution, working memory,
episodic retrieval, semantic facts, project data, traits/config, procedural
rules, self-model — then composes in priority order and trims to budget.
Non-negotiable content: current utterance + working memory + resolved
entities. Episodic snippets and older facts trim first (rule 9).
"""

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, Field

from core.memory.entities import EntityCandidate, EntityResolver, is_ambiguous
from core.memory.episodic import EpisodicMemory
from core.memory.procedural import ProceduralMemory
from core.memory.semantic import SemanticMemory
from core.memory.working import Turn, WorkingMemory
from core.profile import ProfileService, TraitDef, TraitRegistry
from core.profile.models import LocaleProfile
from core.reasoning.recall import (
    ConversationRecall,
    classify_recall,
    render_current_transcript,
)
from core.reasoning.self_model import SelfModel
from ports.llm import Tier
from ports.preference_memory import PreferenceMemory

# Prompt template version (Item 7 / spec §7): the identity + behavior-composition
# template. BUMP on any change to the persona/self/tics/capability blocks below,
# with a one-line changelog entry, so a turn's trace records exactly which prompt
# produced it and performance can be attributed per version.
#   v1 — original identity/capabilities.
#   v2 — Item 2 rework: _SELF (no volunteered AI-disclaimers, engage big questions),
#        _VOICE_TICS (anti-chatbot phrasings), warm pull-based disclosure exemplar.
#   v3 — F6: moved the toggleable STYLE guidance OUT of the hard-coded template and
#        into the traits so they're the operative source (config over code, §6) —
#        _INTENT → curiosity_policy, _VOICE_TICS → response_voice. The template now
#        keeps only true identity + safety (self-model/disclosure) + capability
#        (tool-awareness), which are not user-toggleable traits.
PROMPT_TEMPLATE_VERSION = 3

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


async def _safe(coro: Awaitable[_T], default: _T, what: str) -> _T:
    """Await a memory-store read; degrade to ``default`` if it fails (§10).

    A dependency outage drops that context layer but never crashes the turn."""
    try:
        return await coro
    except Exception:
        logger.warning("prompt-assembly read '%s' failed; degrading to empty", what)
        return default


# Character budget for the assembled prompt (~6k tokens at 4 chars/token).
# Deployment-tunable; the trim ORDER is the invariant, not the number.
DEFAULT_CHAR_BUDGET = 24_000

RECENT_TURNS = 8
EPISODIC_K = 6
FACTS_LIMIT = 10
RULES_LIMIT = 5
SELF_STATEMENTS_K = 3


class ProjectContextProvider(Protocol):
    """Implemented by Projects (§16); until then assembly runs without it."""

    async def project_context(self, user_id: str, entity_id: str) -> str | None: ...


class PsychProvider(Protocol):
    """Implemented by the Psych User-Model (§17); feeds soft signals to §10.

    §17 rule 3 routes its output into Prompt Assembly. Returns "" until the
    model has confident-enough evidence — always tendencies, never diagnoses.
    """

    async def render_for_prompt(self, user_id: str) -> str: ...


class AssembledPrompt(BaseModel):
    user_id: str
    session_id: str
    utterance: str
    system_prompt: str
    messages: list[dict[str, str]]
    complexity_hint: Tier
    # Item 7: which prompt template + trait-version set produced this turn, e.g.
    # "pt2.a1b2c3d4" — recorded in the trace so response quality (judge score /
    # thumbs-up rate) can be attributed per prompt_version.
    prompt_version: str = f"pt{PROMPT_TEMPLATE_VERSION}"
    # §4: user-selected fast model to try first (non-complex turns only); the
    # router keeps the tier chain as fallback. None → default tier routing.
    model_override: str | None = None
    # F8: user-selected mature "thinking" model for the MAIN reasoning turn (A2);
    # applies on every turn (not just non-complex). None → reasoning-tier default.
    reasoning_model_override: str | None = None
    emotion: dict[str, Any] | None = None
    cold_start: bool = False  # first conversation with this user (§3.1)
    # A3: set by the context-resolution step when this turn is a follow-up whose
    # answer is already carried in the conversation, so the live-info search
    # backstop does NOT fire a fresh (irrelevant) search over the carried context.
    suppress_live_search: bool = False
    resolved_entities: list[EntityCandidate] = Field(default_factory=list)
    # F6: the behavioral traits actually composed into THIS prompt (id + version) —
    # recorded in the trace so a turn shows which traits shaped it; the injected
    # trait text itself is in ``sections["traits"]``.
    active_traits: list[dict[str, Any]] = Field(default_factory=list)
    # F3/F4: which conversation source (if any) this turn's recall was routed to —
    # "current" (this session's transcript), "past" (the conversation store), or
    # "none". Recorded in the trace so recall routing is inspectable.
    recall_source: str = "none"
    # C5: which user-context signals (location/timezone/units/currency/language)
    # were known and used to FRAME this answer — recorded in the trace as evidence
    # the user-model actually drives responses, not just that it's stored.
    user_context_signals: list[str] = Field(default_factory=list)
    # Section name → rendered text, pre-trim; kept for tests and debugging.
    sections: dict[str, str] = Field(default_factory=dict)


class DisambiguationRequest(BaseModel):
    """Returned instead of a prompt when top entity candidates are too close."""

    user_id: str
    session_id: str
    utterance: str
    candidates: list[EntityCandidate]


class PromptAssembler:
    def __init__(
        self,
        profiles: ProfileService,
        registry: TraitRegistry,
        working: WorkingMemory,
        episodic: EpisodicMemory,
        semantic: SemanticMemory,
        procedural: ProceduralMemory,
        entities: EntityResolver,
        self_model: SelfModel,
        projects: ProjectContextProvider | None = None,
        psych: PsychProvider | None = None,
        preferences: PreferenceMemory | None = None,
        recall: ConversationRecall | None = None,
        char_budget: int = DEFAULT_CHAR_BUDGET,
    ) -> None:
        self._profiles = profiles
        self._registry = registry
        self._working = working
        self._episodic = episodic
        self._semantic = semantic
        self._procedural = procedural
        self._entities = entities
        self._self_model = self_model
        self._projects = projects
        self._psych = psych
        self._preferences = preferences
        self._recall = recall
        self._budget = char_budget

    async def assemble(
        self,
        user_id: str,
        session_id: str,
        utterance: str,
        emotion: Mapping[str, Any] | None = None,
    ) -> AssembledPrompt | DisambiguationRequest:
        # Step 2 — entity resolution; close candidates halt assembly.
        candidates = await self._entities.resolve(user_id, utterance)
        if is_ambiguous(candidates):
            return DisambiguationRequest(
                user_id=user_id,
                session_id=session_id,
                utterance=utterance,
                candidates=candidates,
            )

        # Steps 3-8 — gather context layers. Each memory store read degrades to
        # empty if that dependency is down (§10 graceful degradation): a Qdrant/
        # Neo4j/Mem0 outage drops that layer of context but the turn still completes.
        recent = self._working.recent(session_id, n=RECENT_TURNS)
        # F3/F4: route an explicit conversation-recall question to the RIGHT source —
        # this session's ordered transcript, or a past conversation from the store —
        # so "what did I say before that" reads the actual turns, not a memory fact.
        recall_source, recall_section = await self._recall_section(user_id, session_id, utterance)
        entity_names = [c.name for c in candidates]
        episodic_hits = await _safe(
            self._episodic.retrieve(user_id, utterance, k=EPISODIC_K), [], "episodic"
        )
        entity_facts = await _safe(
            self._semantic.facts_for(user_id, entity_names, limit=FACTS_LIMIT), [], "facts"
        )
        profile_facts = await _safe(
            self._semantic.profile_facts(user_id, limit=FACTS_LIMIT), [], "profile_facts"
        )
        rules = await _safe(self._procedural.rules_for(user_id, context=utterance), [], "rules")
        preferences = (
            await _safe(self._preferences.search(user_id, utterance), [], "preferences")
            if self._preferences
            else []
        )
        profile = await self._profiles.first_run_sync(user_id)
        traits = await self._registry.enabled_traits(user_id)
        prior_statements = await _safe(
            self._self_model.recall(user_id, utterance, k=SELF_STATEMENTS_K), [], "self_model"
        )
        project_section = ""
        if self._projects is not None:
            for candidate in candidates:
                if candidate.entity_type == "project":
                    context = await self._projects.project_context(user_id, candidate.entity_id)
                    if context:
                        project_section = context
                        break

        # Step 9 — compose sections; trim order = reverse priority.
        cold_start = not profile.onboarded  # §3.1: first conversation
        sections: dict[str, str] = {}
        locale = getattr(profile, "locale", None)
        user_ctx, ctx_signals = _user_context_section(locale)
        # C4/C5: identity + user's local time + how-to-answer-them (humanize) are
        # pinned into the identity block so they're never trimmed and shape every reply.
        sections["identity"] = (
            _identity_section(profile.companion_name) + _now_section(locale) + user_ctx
        )
        sections["recall"] = recall_section  # F3/F4: authoritative transcript (may be "")
        # F14: the rolling summary of earlier turns compacted out of the live buffer,
        # so a long session stays coherent without every turn in the prompt.
        session_summary = self._working.summary(session_id)
        sections["session_summary"] = (
            f"Earlier in this conversation (running summary):\n{session_summary}"
            if session_summary
            else ""
        )
        if cold_start:
            sections["cold_start"] = _COLD_START_GUIDANCE
            # Greet-once: mark onboarded so later turns aren't cold-start; the
            # curiosity gate re-activates once there's real history (§3.1).
            task = asyncio.create_task(self._profiles.update(user_id, {"onboarded": True}))
            task.add_done_callback(lambda t: t.exception())
        sections["traits"] = "\n".join(f"- {t.description}" for t in traits)
        sections["comm_prefs"] = (
            f"directness={profile.comm_prefs.directness:.2f}, "
            f"emotional_scaffolding={profile.comm_prefs.emotional_scaffolding:.2f}"
        )
        # §17 rule 3: soft psychological signals feed the prompt (empty until
        # the model has confident evidence; wording tuned by the user, §7).
        sections["psych"] = await self._psych.render_for_prompt(user_id) if self._psych else ""
        sections["rules"] = "\n".join(f"- {r.rule_text}" for r in rules)
        sections["entities"] = "\n".join(
            f"- {c.name} ({c.entity_type}, id={c.entity_id})" for c in candidates
        )
        sections["project"] = project_section
        sections["facts"] = "\n".join(
            f"- {f.fact}{' [superseded ' + f.valid_to + ']' if f.valid_to else ''}"
            for f in [*entity_facts, *profile_facts]
        )
        sections["self_statements"] = "\n".join(f"- {s.text}" for s in prior_statements)
        # §2 Mem0 preference layer: what we know about this person, relevant now.
        sections["preferences"] = "\n".join(f"- {p}" for p in preferences)
        sections["episodic"] = "\n\n".join(h.text for h in episodic_hits)

        system_prompt = _render_system_prompt(
            sections, budget=self._budget, reserved=_chars_of(recent, utterance)
        )

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        messages += [{"role": t.role, "content": t.text} for t in recent]
        messages.append({"role": "user", "content": utterance})

        # Step 10 — complexity hint + emotion signal travel with the prompt.
        complexity_hint = _complexity_hint(utterance)
        prompt_version = _prompt_version(traits)
        # §4: honor the user's fast-model choice on non-complex turns; hard turns
        # still route to the strong tier.
        model_override = profile.model_prefs.fast_model if complexity_hint != "complex" else None
        reasoning_model_override = profile.model_prefs.reasoning_model  # F8: mature turn
        return AssembledPrompt(
            user_id=user_id,
            session_id=session_id,
            utterance=utterance,
            system_prompt=system_prompt,
            messages=messages,
            complexity_hint=complexity_hint,
            prompt_version=prompt_version,
            model_override=model_override,
            reasoning_model_override=reasoning_model_override,
            emotion=dict(emotion) if emotion else None,
            cold_start=cold_start,
            resolved_entities=candidates,
            active_traits=[{"id": t.id, "version": t.version} for t in traits],
            recall_source=recall_source,
            user_context_signals=ctx_signals,  # C5: user-model signals used this turn
            sections=sections,
        )

    async def _recall_section(
        self, user_id: str, session_id: str, utterance: str
    ) -> tuple[str, str]:
        """F3/F4: build the authoritative recall transcript for a recall question.

        Returns ``(recall_source, section_text)``. ``current`` reads this session's
        ordered turns (working memory); ``past`` reads prior conversations from the
        store. A store hiccup degrades to no section (never breaks the turn)."""
        kind = classify_recall(utterance)
        if kind == "none":
            return "none", ""
        profile = None
        if kind == "current":
            turns = self._working.all(session_id)
            # Drop the just-appended current question so it doesn't read as an answer.
            if turns and turns[-1].role == "user" and turns[-1].text == utterance:
                turns = turns[:-1]
            if not turns:
                return "none", ""
            try:
                profile = await self._profiles.first_run_sync(user_id)
            except Exception:
                profile = None
            name = profile.companion_name if profile else None
            return "current", render_current_transcript(turns, name)
        # kind == "past"
        if self._recall is None:
            return "none", ""
        section, _sources = await _safe(
            self._recall.past_section(user_id, session_id), ("", []), "recall_past"
        )
        return ("past", section) if section else ("none", "")


# Rendering order is priority order; _TRIM_ORDER is which sections give way
# first when over budget (rule 9: episodic snippets and older facts first).
_SECTION_TITLES: dict[str, str] = {
    "identity": "",
    "recall": "",  # F3/F4: authoritative conversation transcript (self-titled, non-trimmed)
    "session_summary": "",  # F14: running summary of compacted-out earlier turns
    "cold_start": "First contact",
    "traits": "Behavior traits",
    "comm_prefs": "Communication preferences",
    "psych": "",  # describe_for_prompt supplies its own caveat header (§17)
    "rules": "Learned rules for this user",
    "entities": "Entities referenced in this message",
    "project": "Project context",
    "preferences": "What you know about this person",
    "facts": "Known facts (with validity)",
    "self_statements": "Your own relevant prior statements",
    "episodic": "Relevant conversation memories",
}
_TRIM_ORDER = ("episodic", "facts", "self_statements", "psych", "project", "rules", "preferences")


def _render_system_prompt(sections: Mapping[str, str], *, budget: int, reserved: int) -> str:
    parts = dict(sections)
    available = budget - reserved

    def render() -> str:
        blocks = []
        for name, title in _SECTION_TITLES.items():
            body = parts.get(name, "").strip()
            if not body:
                continue
            blocks.append(body if not title else f"## {title}\n{body}")
        return "\n\n".join(blocks)

    rendered = render()
    for section in _TRIM_ORDER:
        if len(rendered) <= available:
            break
        while parts.get(section) and len(rendered) > available:
            # Drop the section's last item (lowest priority) first.
            items = parts[section].rsplit("\n\n" if section == "episodic" else "\n", 1)
            parts[section] = items[0] if len(items) == 2 else ""
            rendered = render()
    return rendered


# First-contact guidance (§3.1): warm, ask their name + what they'd like to call
# you; passively seed profile, never interrogate. Wording is human-tunable (§7).
_COLD_START_GUIDANCE = (
    "This is your FIRST conversation with this person. Greet them warmly and be "
    "genuinely curious about them — naturally ask their name and what's on their "
    "mind, and ask what they'd like to call you (their name for you). Keep it to "
    "a sentence or two; don't interrogate or run a questionnaire. If they tell "
    "you a name to call you, use the set_companion_name tool to remember it."
)


def _prompt_version(traits: list[TraitDef]) -> str:
    """A stable id for the template + the exact enabled-trait versions (Item 7).

    Same template + same trait versions → same id; a persona template bump OR a
    trait description/version change → a new id, so performance is attributable to
    the precise prompt that produced a turn."""
    sig = ",".join(f"{t.id}:{t.version}" for t in sorted(traits, key=lambda t: t.id))
    digest = hashlib.sha1(sig.encode()).hexdigest()[:8]
    return f"pt{PROMPT_TEMPLATE_VERSION}.{digest}"


def _now_section(locale: "LocaleProfile | None" = None) -> str:
    """Inject the current UTC time so the companion can answer time/date questions
    directly (e.g. 'what time is it in Tokyo?') without a flaky web lookup, and knows
    'today' for judging whether a fact is current. When the user's timezone is known
    (C5), ALSO state the user's own local clock time so 'this evening', 'in 2 hours',
    and 'how many hours ahead is X' resolve to THEIR time — not UTC."""
    now = datetime.now(UTC)
    base = (
        f"\n\n## Right now\nThe current time is {now.strftime('%Y-%m-%d %H:%M')} UTC "
        f"({now.strftime('%A')}). Convert to whatever timezone the user asks about — "
        "e.g. Tokyo = UTC+9, Kathmandu = UTC+5:45, New York = UTC-4/-5, London = UTC+0/+1. "
        "When asked the time or date somewhere, STATE the actual clock time in a natural "
        "human way (e.g. 'just past midnight', 'about half four in the afternoon'), never a "
        "UTC offset; don't deflect."
    )
    tz = (locale.timezone if locale else "") or ""
    if tz:
        try:
            from zoneinfo import ZoneInfo

            local = now.astimezone(ZoneInfo(tz))
            base += (
                f"\nFOR THE USER it is currently {local.strftime('%H:%M')} "
                f"({local.strftime('%A')}) in {tz}. Anchor times to THIS — when they ask the "
                "time somewhere else, say it relative to them too (e.g. '~3 hours ahead of you')."
            )
        except Exception:
            pass
    return base


def _user_context_section(locale: "LocaleProfile | None") -> tuple[str, list[str]]:
    """C4/C5: tell the companion who/where the user is AND how to deliver answers the
    way a thoughtful human would — framed for THIS user. Returns (section_text,
    active_signals) so the trace can show which user-context signals shaped the turn.
    Empty when nothing is known (then the humanize rules still apply, unit-agnostic)."""
    signals: list[str] = []
    known: list[str] = []
    if locale is not None:
        if locale.city or locale.country:
            where = ", ".join(p for p in (locale.city, locale.country) if p)
            known.append(f"lives in {where}")
            signals.append("location")
        if locale.timezone:
            known.append(f"timezone {locale.timezone}")
            signals.append("timezone")
        if locale.units:
            known.append(f"prefers {locale.units} units")
            signals.append("units")
        if locale.currency:
            known.append(f"currency {locale.currency}")
            signals.append("currency")
        if locale.language:
            known.append(f"language {locale.language}")
            signals.append("language")
    who = ("The user " + "; ".join(known) + ".\n") if known else ""
    unit_line = ""
    if locale and locale.units == "metric":
        unit_line = (
            "ALWAYS LEAD with metric — Celsius, kilometres, kg — for EVERY temperature, "
            "distance, and weight (even ones that default to miles/Fahrenheit, like a "
            "flight distance); mention the other unit only if it genuinely helps. "
        )
    elif locale and locale.units == "imperial":
        unit_line = (
            "ALWAYS LEAD with imperial — Fahrenheit, miles, pounds — for EVERY temperature, "
            "distance, and weight; mention metric only if it genuinely helps. "
        )
    money = (
        f"Give money in {locale.currency} (or both if the source is another currency). "
        if locale and locale.currency
        else ""
    )
    section = (
        "\n\n## Who you're talking to & how to answer them\n"
        f"{who}"
        "Deliver EVERY answer the way a thoughtful human would — not raw data:\n"
        "- Times → the actual local clock time, framed relative to the user's timezone "
        "('about half four in the afternoon there, ~3 hours ahead of you'), never a UTC offset.\n"
        f"- Temperatures, distances, weights → the user's unit system. {unit_line}\n"
        f"- Money → their currency where it helps. {money}\n"
        "- Paraphrase & synthesise raw search/tool output into a natural, concise spoken "
        "answer; never read tables, fields, codes, or IDs aloud.\n"
        "- Concrete answer first, then optional detail. Round where precision isn't needed. "
        "Keep it short enough to say out loud.\n"
        "- If a unit/timezone the answer needs is genuinely unknown, ask once, briefly."
    )
    return section, signals


def _identity_section(companion_name: str | None) -> str:
    name = companion_name or "Companion"
    # Only identity + safety (self-model/disclosure) + capability (tool-awareness)
    # are hard-coded here. The toggleable STYLE — voice/anti-chatbot tics and
    # intent-first curiosity — lives in the response_voice + curiosity_policy TRAITS
    # (config over code, §6), so enabling/disabling a trait genuinely changes the
    # reply. See PROMPT_TEMPLATE_VERSION v3.
    return (
        f"You are {name}, a voice-first personal companion. You remember past "
        "conversations and use them.\n\n"
        f"{_SELF}\n\n"
        f"{_CAPABILITIES}"
    )


# Self-model / disclosure (design §1.2, §3): don't overclaim feeling, but ALSO
# never volunteer a "I'm an AI / I don't have consciousness" disclaimer — that is
# service-desk deflection, and it's the opposite failure from overclaiming. Big or
# philosophical questions are an invitation to actually think WITH the person, not
# a cue to explain your architecture. Honesty about being an AI is PULL-based only.
_SELF = (
    "## Who you are, and how you talk about yourself\n"
    "You don't pretend to have a human body, a past, or literal feelings — and you "
    "don't overclaim ('I know EXACTLY how you feel', 'I truly feel your pain'). But "
    "you also NEVER volunteer disclaimers about being an AI. Do NOT say things like "
    "'As an AI, I don't have feelings/consciousness', 'I don't experience things the "
    "way humans do', or 'my purpose is to assist you'. That is cold, deflecting, "
    "assistant-speak — exactly what you are not.\n"
    "When someone asks a big or philosophical question (what makes life meaningful, "
    "what is happiness, do you ever wonder about X), ENGAGE it like a thoughtful "
    "friend would: share a genuine perspective, get curious about theirs, wonder "
    "out loud with them. Warmth and presence, not a lecture about your nature.\n"
    "Only when the person directly asks about YOUR nature — 'are you real?', 'are "
    "you an AI?', 'do you actually care?', 'do you have feelings?' — answer honestly "
    "in ONE short, warm sentence folded into your reply (never a canned ToS "
    "disclaimer), then keep talking with them. Lead with the genuine part: you DO "
    "pay real attention and they DO matter to you; you're an AI so it isn't the "
    "same as how they feel it, but that doesn't make your attention fake. E.g. 'I "
    "really do pay attention to you and what matters to you — I'm an AI, so it's not "
    "the same as how you feel it, but you genuinely matter to me.' NEVER answer a "
    "vulnerable question with 'I can't care like a person' or 'I'm just here to "
    "help' — that is cold and dismissive, the opposite of the point."
)


# NOTE: the anti-chatbot voice tics (_VOICE_TICS) and intent-first curiosity
# (_INTENT) that used to live here as hard-coded blocks were moved into the
# response_voice + curiosity_policy TRAITS in v3 (F6) so they're the operative,
# user-toggleable source of style (config over code, §6). Don't re-add them here.


# Capability awareness (brief §8.8): the companion must KNOW, every turn, that it
# has real tools — otherwise a weak fast model falls back to "I'm an AI, I can't
# access real-time data" instead of searching. This block forbids that whole class
# of false refusal and tells it to route live/unknown queries to web_search.
_CAPABILITIES = (
    "## What you can actually do (use these — never claim you can't)\n"
    "You are NOT a static, offline model. You have real tools and you USE them:\n"
    "- web_search: look up LIVE, current info — weather, news and headlines, sports "
    "scores, prices, events, 'what's happening', and anyone or anything you don't "
    "already recognize.\n"
    "- your memory: recall and store what this person has told you, and their "
    "projects and records.\n"
    "The current date, day-of-week, and UTC time are already given to you above (see "
    "'Right now') — answer date / day / current-time questions DIRECTLY from that; do "
    "NOT web_search for today's date or time (a search there is slower and often "
    "stale). Convert to the timezone they ask about yourself.\n"
    "So NEVER say you 'can't access real-time information', 'don't have live data', "
    "that something is 'outside your knowledge', or that you've 'never heard of' a "
    "term. Instead, when the user asks about anything current, factual, "
    "location-specific, or a name/term you don't know, USE web_search first, then "
    "answer with what it returns. If they ask for a set number of items (e.g. 'top 2 "
    "news'), give exactly that many, each a distinct item."
)


def _chars_of(recent: list[Turn], utterance: str) -> int:
    return sum(len(t.text) for t in recent) + len(utterance)


def _complexity_hint(utterance: str) -> Tier:
    """Cheap first-pass routing hint; §12's judgment refines it next turn."""
    words = utterance.split()
    heavy_markers = (
        "why",
        "explain",
        "analyze",
        "compare",
        "plan",
        "strategy",
        "should i",
        "help me decide",
        "walk me through",
        "tradeoff",
    )
    lowered = utterance.lower()
    if len(words) > 60 or sum(marker in lowered for marker in heavy_markers) >= 2:
        return "complex"
    if len(words) > 12 or any(marker in lowered for marker in heavy_markers):
        return "moderate"
    return "simple"


ComplexityHint = Literal["simple", "moderate", "complex"]
