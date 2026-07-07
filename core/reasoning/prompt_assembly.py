"""Prompt Assembly (spec §10): utterance → final LLM prompt, or a disambiguation halt.

Runs the ordered context pipeline — entity resolution, working memory,
episodic retrieval, semantic facts, project data, traits/config, procedural
rules, self-model — then composes in priority order and trims to budget.
Non-negotiable content: current utterance + working memory + resolved
entities. Episodic snippets and older facts trim first (rule 9).
"""

import asyncio
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from core.memory.entities import EntityCandidate, EntityResolver, is_ambiguous
from core.memory.episodic import EpisodicMemory
from core.memory.procedural import ProceduralMemory
from core.memory.semantic import SemanticMemory
from core.memory.working import Turn, WorkingMemory
from core.profile import ProfileService, TraitRegistry
from core.reasoning.self_model import SelfModel
from ports.llm import Tier
from ports.preference_memory import PreferenceMemory

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
    # §4: user-selected fast model to try first (non-complex turns only); the
    # router keeps the tier chain as fallback. None → default tier routing.
    model_override: str | None = None
    emotion: dict[str, Any] | None = None
    cold_start: bool = False  # first conversation with this user (§3.1)
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

        # Steps 3-8 — gather context layers.
        recent = self._working.recent(session_id, n=RECENT_TURNS)
        episodic_hits = await self._episodic.retrieve(user_id, utterance, k=EPISODIC_K)
        entity_names = [c.name for c in candidates]
        entity_facts = await self._semantic.facts_for(user_id, entity_names, limit=FACTS_LIMIT)
        profile_facts = await self._semantic.profile_facts(user_id, limit=FACTS_LIMIT)
        rules = await self._procedural.rules_for(user_id, context=utterance)
        preferences = (
            await self._preferences.search(user_id, utterance) if self._preferences else []
        )
        profile = await self._profiles.first_run_sync(user_id)
        traits = await self._registry.enabled_traits(user_id)
        prior_statements = await self._self_model.recall(user_id, utterance, k=SELF_STATEMENTS_K)
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


def _identity_section(companion_name: str | None) -> str:
    name = companion_name or "Companion"
    return (
        f"You are {name}, a voice-first personal companion. Warm, natural, "
        "concise — you talk like a person, not an assistant. You remember "
        "past conversations and use them. You never claim to be conscious "
        "or to feel emotions; you validate without overclaiming."
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
