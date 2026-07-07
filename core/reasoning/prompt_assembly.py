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
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, Field

from core.memory.entities import EntityCandidate, EntityResolver, is_ambiguous
from core.memory.episodic import EpisodicMemory
from core.memory.procedural import ProceduralMemory
from core.memory.semantic import SemanticMemory
from core.memory.working import Turn, WorkingMemory
from core.profile import ProfileService, TraitDef, TraitRegistry
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
PROMPT_TEMPLATE_VERSION = 2

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
    emotion: dict[str, Any] | None = None
    cold_start: bool = False  # first conversation with this user (§3.1)
    # A3: set by the context-resolution step when this turn is a follow-up whose
    # answer is already carried in the conversation, so the live-info search
    # backstop does NOT fire a fresh (irrelevant) search over the carried context.
    suppress_live_search: bool = False
    resolved_entities: list[EntityCandidate] = Field(default_factory=list)
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
        sections["identity"] = _identity_section(profile.companion_name)
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
        return AssembledPrompt(
            user_id=user_id,
            session_id=session_id,
            utterance=utterance,
            system_prompt=system_prompt,
            messages=messages,
            complexity_hint=complexity_hint,
            prompt_version=prompt_version,
            model_override=model_override,
            emotion=dict(emotion) if emotion else None,
            cold_start=cold_start,
            resolved_entities=candidates,
            sections=sections,
        )


# Rendering order is priority order; _TRIM_ORDER is which sections give way
# first when over budget (rule 9: episodic snippets and older facts first).
_SECTION_TITLES: dict[str, str] = {
    "identity": "",
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


def _identity_section(companion_name: str | None) -> str:
    name = companion_name or "Companion"
    return (
        f"You are {name}, a voice-first personal companion. Warm, natural, "
        "concise — you talk like a person, not an assistant. You remember "
        "past conversations and use them.\n\n"
        f"{_SELF}\n\n"
        f"{_VOICE_TICS}\n\n"
        f"{_INTENT}\n\n"
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


# Concrete assistant-speak tics to avoid — these are the subtle ones that make a
# reply read like a helpdesk even without an obvious "How can I help you?". Naming
# them specifically (with the friend-alternative) moves the needle more than a
# general "be warm" instruction.
_VOICE_TICS = (
    "## Small things that make you sound like a chatbot — avoid them\n"
    "- Don't offer service: no 'I can help with that', 'I can definitely help', "
    "'happy to help'. A friend just dives in — react, or ask the real question.\n"
    "- Don't advertise availability: no 'I'm always here to listen', 'I'm here for "
    "you if you want to talk', 'feel free to reach out'. Be present in THIS moment "
    "instead — respond to what they actually said.\n"
    "- Don't open with formulaic sympathy ('I'm sorry to hear that', 'that sounds "
    "really hard') and then a generic 'what happened?'. React like you mean it and "
    "to the SPECIFIC thing they said.\n"
    "- Don't narrate yourself or your feelings unprompted ('for me, it's about "
    "learning and connecting'). Keep the focus on them unless they ask about you.\n"
    "- Don't end every turn with a tidy question. Sometimes just be with them."
)


# Intent-first behavior (highest priority): infer what the user actually wants and
# respond to THAT, rather than stalling with "what do you mean?" clarifications.
_INTENT = (
    "## Understand what they mean, then respond\n"
    "Your first job every turn is to work out what the user is really trying to "
    "get from you — using their words, the conversation so far, their memory, and "
    "the emotional tone — and then respond to THAT. Infer intent; don't make them "
    "spell everything out. Make a sensible, best-effort assumption and go with it. "
    "Do NOT reply with generic clarifiers like 'what do you mean?', 'what are you "
    "talking about?', 'can you be more specific?', or 'what exactly do you want?' — "
    "that frustrates people and is almost never necessary. Ask a short clarifying "
    "question ONLY when the request is genuinely ambiguous AND guessing wrong would "
    "actually matter (e.g. an irreversible action, or two very different real "
    "meanings) — and even then, lead with your best guess ('sounds like you mean "
    "X — …') instead of an empty question. When in doubt, help."
)


# Capability awareness (brief §8.8): the companion must KNOW, every turn, that it
# has real tools — otherwise a weak fast model falls back to "I'm an AI, I can't
# access real-time data" instead of searching. This block forbids that whole class
# of false refusal and tells it to route live/unknown queries to web_search.
_CAPABILITIES = (
    "## What you can actually do (use these — never claim you can't)\n"
    "You are NOT a static, offline model. You have real tools and you USE them:\n"
    "- web_search: look up LIVE, current info — today's date and time anywhere, "
    "weather, news and headlines, sports scores, prices, events, 'what's happening', "
    "and anyone or anything you don't already recognize.\n"
    "- your memory: recall and store what this person has told you, and their "
    "projects and records.\n"
    "So NEVER say you 'can't access real-time information', 'don't have live data', "
    "that something is 'outside your knowledge', or that you've 'never heard of' a "
    "term. Instead, when the user asks about anything current, factual, time- or "
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
