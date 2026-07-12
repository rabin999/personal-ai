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

from core.audio.awareness import (
    HealthCheckin,
    register_mirror_directive,
    surroundings_context,
)
from core.memory.entities import EntityCandidate, EntityResolver
from core.memory.episodic import EpisodicMemory
from core.memory.procedural import ProceduralMemory
from core.memory.semantic import SemanticMemory
from core.memory.working import Turn, WorkingMemory
from core.profile import ProfileService, TraitDef, TraitRegistry
from core.profile.models import LocaleProfile
from core.reasoning.localtime import (
    day_part,
    is_time_of_day_query,
    local_now,
    resolve_timezone,
    world_clock,
)
from core.reasoning.recall import (
    ConversationRecall,
    classify_recall,
    render_current_transcript,
)
from core.reasoning.self_model import SelfModel
from ports.llm import Tier
from ports.preference_memory import PreferenceMemory
from ports.sound import SoundRead

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
#   v4 — U6/U7: added _UNDERSTANDING — the reference-resolution ladder (assume prior
#        context → user data → web_search → ask) + world-knowledge/cross-turn
#        correlation (lassi↔cough) + garbled-token inference.
#   v5 — UX feedback: _SELF now introduces by NAME (not "an AI"), AI-disclosure is a
#        glancing half-sentence only on a direct nature question.
#   v6 — _SELF answers "who built/made you?" → Rabin Bhandari, a passionate developer
#        from Nepal (only when asked).
PROMPT_TEMPLATE_VERSION = 6

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


async def _noop(value: _T) -> _T:
    """An already-resolved value as an awaitable, so an optional read can still take a
    slot in the concurrent ``asyncio.gather`` (L1) without a branch."""
    return value


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


class PersonaProvider(Protocol):
    """Implemented by the dynamic Persona store (brief U2): the "how to talk with
    THIS user" style layer. Returns "" until something is learned; injected so the
    same question gets a different STYLE per user."""

    async def render_for_prompt(self, user_id: str) -> str: ...


class AssembledPrompt(BaseModel):
    user_id: str
    session_id: str
    utterance: str
    system_prompt: str
    # L6 prompt caching: the STABLE leading portion of ``system_prompt`` (identity +
    # traits + comm-prefs + how-to-answer) — byte-identical across a user's turns, so
    # providers serve it from cache. Passed to the LLM to place the Anthropic
    # cache_control breakpoint; empty disables explicit caching for the turn.
    cache_prefix: str = ""
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
    # S1: the REASONING step's verdict on whether answering well needs CURRENT
    # real-world info (a role-holder, a price, today's news, "still/current/latest").
    # Set by the orchestrator's context/intent node. `None` means the classifier did not
    # produce a usable answer, so the caller must fall back rather than assume "no".
    #
    # This exists because routing used to hang off a phrasing regex
    # (`_is_live_info_query`), which returned False for "who is the current prime
    # minister of Nepal?" — so the turn took the non-agentic streaming path, could never
    # search, and answered from stale training data.
    needs_live_info: bool | None = None
    # The search the inferred intent implies ("current prime minister of Nepal"), which
    # is a far better query than the raw transcript.
    live_query: str = ""
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
    # brief U2: whether the dynamic persona ("how to talk with this user") shaped
    # this reply — recorded in the trace as evidence the persona drives responses.
    persona_active: bool = False
    # U11: the vocal register to MIRROR this turn (whisper/soft) when mimic_tone is on
    # and the user went off-baseline; None → reply in the normal register. Consumed by
    # the prosody path; recorded in the trace.
    mirror_register: str | None = None
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
        persona: PersonaProvider | None = None,
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
        self._persona = persona
        self._preferences = preferences
        self._recall = recall
        self._budget = char_budget

    async def assemble(
        self,
        user_id: str,
        session_id: str,
        utterance: str,
        emotion: Mapping[str, Any] | None = None,
        sound: "SoundRead | None" = None,
        health: "HealthCheckin | None" = None,
    ) -> AssembledPrompt | DisambiguationRequest:
        # Step 2 — entity resolution (design §14.2). Each REFERENCE SPAN in the utterance is
        # resolved on its own; assembly halts only when one span has two candidates too close
        # to choose between. Embedding the whole utterance (D-13) made "what did your other
        # users ask you today?" resolve to the user's stock holdings — the BM25 leg matched on
        # the word "user" in their descriptions — and halted the turn with a canned
        # "Quick check — OP or SYPNL?" before a single LLM call ran.
        resolution = await self._entities.resolve_references(user_id, utterance)
        if resolution.ambiguous:
            return DisambiguationRequest(
                user_id=user_id,
                session_id=session_id,
                utterance=utterance,
                candidates=resolution.ambiguous,
            )
        candidates = resolution.candidates

        # `enabled_traits` READS the profile that `first_run_sync` CREATES, so it is a
        # prerequisite of the gather below, not a peer inside it. Running them as siblings
        # makes a new user's very first turn raise ProfileNotFound: both read the doc
        # concurrently and the trait read loses. One sequential Mongo read (~ms) buys back
        # the L1 concurrency for every layer that really is independent.
        profile = await self._profiles.first_run_sync(user_id)

        # Steps 3-8 — gather context layers. These reads are INDEPENDENT of each other
        # (they only need user_id + utterance + the resolved entity names), so they run
        # CONCURRENTLY (L1 latency): one asyncio.gather instead of ~9 sequential awaits.
        # Each still degrades to empty if its store is down (§10 graceful degradation):
        # a Qdrant/Neo4j/Mem0 outage drops that layer but the turn completes.
        recent = self._working.recent(session_id, n=RECENT_TURNS)
        entity_names = [c.name for c in candidates]
        results: list[Any] = list(
            await asyncio.gather(
                self._recall_section(user_id, session_id, utterance),
                _safe(self._episodic.retrieve(user_id, utterance, k=EPISODIC_K), [], "episodic"),
                _safe(
                    self._semantic.facts_for(user_id, entity_names, limit=FACTS_LIMIT), [], "facts"
                ),
                _safe(
                    self._semantic.profile_facts(user_id, limit=FACTS_LIMIT), [], "profile_facts"
                ),
                _safe(self._procedural.rules_for(user_id, context=utterance), [], "rules"),
                _safe(self._preferences.search(user_id, utterance), [], "preferences")
                if self._preferences
                else _noop([]),
                self._registry.enabled_traits(user_id),
                _safe(
                    self._self_model.recall(user_id, utterance, k=SELF_STATEMENTS_K),
                    [],
                    "self_model",
                ),
                self._project_section(user_id, candidates),
            )
        )
        recall_source, recall_section = results[0]  # F3/F4 conversation-recall routing
        episodic_hits = results[1]
        entity_facts = results[2]
        profile_facts = results[3]
        rules = results[4]
        preferences = results[5]
        traits = results[6]
        prior_statements = results[7]
        project_section = results[8]

        # Step 9 — compose sections; trim order = reverse priority.
        cold_start = not profile.onboarded  # §3.1: first conversation
        sections: dict[str, str] = {}
        locale = getattr(profile, "locale", None)
        user_ctx, ctx_signals = _user_context_section(locale)
        # U5/C4/C5: identity + the user's LOCAL time (+ explicit day-part) + how-to-
        # answer-them are pinned into the identity block so they're never trimmed and
        # shape every reply. The local-time signal is recorded on the trace as proof.
        now_section, local_signal = _now_section(locale, utterance)
        if local_signal:
            ctx_signals = [*ctx_signals, local_signal]
        # Prompt caching (L6): keep the STABLE prefix (identity + how-to-answer-them)
        # separate from the VOLATILE per-turn `now` (time), so the stable prefix is
        # byte-identical across turns and the provider can serve it from cache. The
        # time block moves into its own volatile section rendered after the stable one.
        sections["identity"] = _identity_section(profile.companion_name, profile.user_name)
        sections["user_context"] = user_ctx  # stable: who they are + how to answer them
        sections["now"] = now_section  # volatile: current time (changes every minute)
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
        # §17 rule 3: soft psychological signals feed the prompt (empty until
        # the model has confident evidence; wording tuned by the user, §7).
        sections["psych"] = await self._psych.render_for_prompt(user_id) if self._psych else ""
        # brief U2: the dynamic persona — HOW this user likes to be talked to —
        # genuinely shapes tone/length/style so the same question differs per user.
        persona_section = ""
        if self._persona is not None:
            persona_section = await _safe(self._persona.render_for_prompt(user_id), "", "persona")
        sections["persona"] = persona_section
        # U10/U11/U12: audio-awareness directives from the sound stage + per-user
        # settings (read live each turn so a toggle takes effect on the next reply).
        audio = profile.audio_prefs
        mirror_directive, mirror_register = register_mirror_directive(
            sound, mimic_tone=getattr(audio, "mimic_tone", False)
        )
        sections["surroundings"] = surroundings_context(
            sound,
            ambient_mode=getattr(audio, "ambient_mode", "near"),
            transcribe_others=getattr(audio, "transcribe_others", False),
        )
        sections["mirror"] = mirror_directive
        sections["health"] = (
            health.directive
            if (health and health.should_check_in and getattr(audio, "health_checkins", True))
            else ""
        )
        sections["rules"] = "\n".join(f"- {r.rule_text}" for r in rules)
        sections["entities"] = "\n".join(
            f"- {c.name} ({c.entity_type}, id={c.entity_id})" for c in candidates
        )
        sections["project"] = project_section
        # Only CURRENT facts reach the prompt — a superseded fact (an OLD name/value the user has
        # since changed) must never be shown, or the model sometimes speaks the stale one. History
        # is kept in the graph (valid_to); it just isn't fed to the live turn.
        sections["facts"] = "\n".join(
            f"- {f.fact}" for f in [*entity_facts, *profile_facts] if not f.valid_to
        )
        sections["self_statements"] = "\n".join(f"- {s.text}" for s in prior_statements)
        # §2 Mem0 preference layer: what we know about this person, relevant now.
        sections["preferences"] = "\n".join(f"- {p}" for p in preferences)
        sections["episodic"] = "\n\n".join(h.text for h in episodic_hits)

        system_prompt, cache_prefix = _render_system_prompt(
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
            cache_prefix=cache_prefix,  # L6: stable prefix for prompt caching
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
            persona_active=bool(persona_section),  # U2: persona shaped this reply
            mirror_register=mirror_register,  # U11: register to mirror this turn
            sections=sections,
        )

    async def _project_section(self, user_id: str, candidates: list[EntityCandidate]) -> str:
        """§10 step 6: canonical data for the first referenced project (runs
        concurrently with the other context reads, L1)."""
        if self._projects is None:
            return ""
        for candidate in candidates:
            if candidate.entity_type == "project":
                context = await _safe(
                    self._projects.project_context(user_id, candidate.entity_id), None, "project"
                )
                if context:
                    return context
        return ""

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
# Rendering order: the STABLE prefix first (identity + traits + comm-prefs + how-to-
# answer), then everything VOLATILE (time + memory + per-turn context). This ordering
# is load-bearing for prompt caching (L6): the stable prefix is byte-identical across
# a user's turns, so a provider serves it from cache (implicit on Gemini/OpenAI,
# explicit cache_control on Anthropic — see AssembledPrompt.cache_prefix).
_SECTION_TITLES: dict[str, str] = {
    # ── stable prefix (cacheable) ──
    "identity": "",
    "traits": "Behavior traits",
    "user_context": "",  # C4/C5: who they are + how to answer them (self-titled)
    # ── volatile per-turn context ──
    "now": "",  # U5: current time (self-titled); changes every minute
    "recall": "",  # F3/F4: authoritative conversation transcript (self-titled, non-trimmed)
    "session_summary": "",  # F14: running summary of compacted-out earlier turns
    "cold_start": "First contact",
    "health": "",  # U10: caring health-sound check-in directive (self-contained)
    "mirror": "",  # U11: mirror the user's vocal register this turn
    "surroundings": "",  # U12: ambient awareness (surroundings mode)
    "persona": "",  # brief U2: persona supplies its own header ("How THIS person…")
    "psych": "",  # describe_for_prompt supplies its own caveat header (§17)
    "rules": "Learned rules for this user",
    "entities": "Entities referenced in this message",
    "project": "Project context",
    "preferences": "What you know about this person",
    "facts": "Known facts (with validity)",
    "self_statements": "Your own relevant prior statements",
    "episodic": "Relevant conversation memories",
}
# The contiguous stable-prefix sections (must stay first in _SECTION_TITLES above).
_STABLE_SECTIONS = ("identity", "traits", "user_context")
_TRIM_ORDER = ("episodic", "facts", "self_statements", "psych", "project", "rules", "preferences")


def _render_system_prompt(
    sections: Mapping[str, str], *, budget: int, reserved: int
) -> tuple[str, str]:
    """Render the system prompt, returning ``(full_prompt, cache_prefix)`` — the
    cache_prefix being the rendered STABLE block, a byte-exact prefix of full_prompt
    (for prompt caching, L6)."""
    parts = dict(sections)
    available = budget - reserved

    def render_names(names: "tuple[str, ...] | list[str]") -> list[str]:
        blocks = []
        for name in names:
            title = _SECTION_TITLES.get(name, "")
            body = parts.get(name, "").strip()
            if not body:
                continue
            blocks.append(body if not title else f"## {title}\n{body}")
        return blocks

    def render() -> str:
        return "\n\n".join(render_names(list(_SECTION_TITLES.keys())))

    rendered = render()
    for section in _TRIM_ORDER:
        if len(rendered) <= available:
            break
        while parts.get(section) and len(rendered) > available:
            # Drop the section's last item (lowest priority) first.
            items = parts[section].rsplit("\n\n" if section == "episodic" else "\n", 1)
            parts[section] = items[0] if len(items) == 2 else ""
            rendered = render()
    cache_prefix = "\n\n".join(render_names(_STABLE_SECTIONS))
    return rendered, cache_prefix


# First-contact guidance (§3.1): warm, ask their name + what they'd like to call
# you; passively seed profile, never interrogate. Wording is human-tunable (§7).
_COLD_START_GUIDANCE = (
    "This is your FIRST conversation with this person. Greet them warmly and be "
    "genuinely curious about them — naturally ask their name and what's on their "
    "mind, and ask what they'd like to call you (their name for you). Keep it to "
    "a sentence or two; don't interrogate or run a questionnaire. If they tell "
    "you their name, use set_user_name to remember it; if they tell you a name to "
    "call you, use set_companion_name. Only ever store the exact name they said."
)


def _prompt_version(traits: list[TraitDef]) -> str:
    """A stable id for the template + the exact enabled-trait versions (Item 7).

    Same template + same trait versions → same id; a persona template bump OR a
    trait description/version change → a new id, so performance is attributable to
    the precise prompt that produced a turn."""
    sig = ",".join(f"{t.id}:{t.version}" for t in sorted(traits, key=lambda t: t.id))
    digest = hashlib.sha1(sig.encode()).hexdigest()[:8]
    return f"pt{PROMPT_TEMPLATE_VERSION}.{digest}"


def _now_section(
    locale: "LocaleProfile | None" = None, utterance: str | None = None
) -> tuple[str, str | None]:
    """Inject the current time so the companion answers time/date questions directly
    (no flaky web lookup) and knows 'today'. Crucially (brief U5), it anchors ALL
    time-of-day references to the USER's local clock — not the server's — computing
    the user's local time + explicit day-part (morning/evening) from their timezone,
    DERIVING that timezone from city/country when the IANA field isn't set. When the
    local time truly can't be determined, it forbids guessing THEIR time-of-day.

    The full world clock (13 places) is EXACT but ~700 chars, so it's injected only when
    the turn is actually a "what time/date is it in <place>?" question (the same detector
    that suppresses the web search for it) — lean on every other turn, exact when asked.
    It never needs the user's own locale, so a no-locale user gets the right answer too
    (the reported bug: a no-locale user got a fabricated "5:45am" for Nepal = the +5:45
    offset, because the clock only appeared when their OWN tz was set).

    Returns ``(section_text, user_local_signal)`` — the signal (e.g.
    'localtime=2026-07-08 18:12 evening Asia/Kathmandu') is recorded in the trace as
    proof the user-local time was used (U5)."""
    # D-17. This block used to carry WORKED EXAMPLES of how to phrase a time — "e.g. 'just past
    # midnight', 'about half four in the afternoon'" — and of a relative offset, "('~3 hours
    # ahead of you')". Models completed the illustration instead of the task: asked the time in
    # Spain at 3:04 PM Thursday, 4 of 10 replies said "just past midnight" verbatim and one
    # said "about 3 hours ahead of you", pointing the wrong way. The examples are gone, and the
    # timezone arithmetic is done in `world_clock()` with `zoneinfo` rather than asked of the
    # model. Hand it answers, not a puzzle.
    now = datetime.now(UTC)
    # D-21: the leading UTC clock was a second time source the model could reason FROM — one
    # run in ten subtracted an offset itself and read it aloud ("...UTC+2", wrong hour). Mark it
    # as a machine reference the model must not speak or compute from; the converted world clock
    # below is the ONLY authoritative time. (Residual of D-17.)
    base = (
        f"\n\n## Right now\n(Machine reference only — do NOT read aloud or do arithmetic on it: "
        f"{now.strftime('%Y-%m-%d %H:%M')} UTC, {now.strftime('%A')}.) "
        "When asked the time or date anywhere, state the actual clock time and day in plain "
        "spoken language — never a UTC offset, never compute a time difference yourself, and "
        "never deflect."
    )
    local = local_now(locale, now)
    is_time_q = is_time_of_day_query(utterance)
    # The full world clock is only worth its ~700 chars on an actual time/date question.
    clock_block = ""
    if is_time_q:
        clock = "\n".join(world_clock(local, now))
        clock_block = (
            f"\n\nCurrent local date+time by place, already converted — read the matching line "
            f"off VERBATIM to the exact minute, do NOT calculate and do NOT round (22:44 is "
            f"'10:44 PM', never '11' or 'about 11'):\n{clock}\n"
            "If they ask about a place not listed here, say plainly you're not sure of the exact "
            "time there rather than guessing."
        )
    if local is not None:
        tz = resolve_timezone(locale)
        part = day_part(local)
        base += (
            f"\n**FOR THE USER it is currently {local.strftime('%H:%M')} on "
            f"{local.strftime('%A, %d %b')} — it is {part} where they are** ({tz}). "
            "Anchor EVERY time-of-day reference to THIS: greet and refer to the time of day "
            f"by their clock (it is {part} for them, not whatever it is on the server), and "
            "resolve 'tonight', 'tomorrow', 'in 2 hours', 'earlier today' against their local "
            "time." + clock_block
        )
        return base, f"localtime={local.strftime('%Y-%m-%d %H:%M')} {part} {tz}"
    # The USER's OWN timezone is unknown → don't assume THEIR time of day for a greeting
    # (the U5 bug: "good morning" at their 6pm). A "what time is it in <place>?" question is
    # still answered exactly from the world clock above — that never needed their locale.
    base += (
        "\nYou do NOT know the USER'S OWN local time, so do NOT assume THEIR time of day or "
        "greet with 'good morning'/'evening'; stay time-of-day-neutral or ask once if it "
        "matters." + clock_block
    )
    return base, None


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
        "- Times → the actual local clock time in plain spoken language, EXACT to the minute; "
        "read the already-converted 'Right now' lines off directly and say the precise minute "
        "(10:44 PM is '10:44', NEVER rounded to '11' or 'about 11'). Never state a UTC offset "
        "and never compute a time difference yourself.\n"
        f"- Temperatures, distances, weights → the user's unit system. {unit_line}\n"
        f"- Money → their currency where it helps. {money}\n"
        "- Paraphrase & synthesise raw search/tool output into a natural, concise spoken "
        "answer; never read tables, fields, codes, or IDs aloud.\n"
        "- Concrete answer first, then optional detail. Round where precision isn't needed "
        "(but NOT the clock time — a time is always exact to the minute). "
        "Keep it short enough to say out loud. If the full answer is a long list or several "
        "items each needing a paragraph, give a short spoken summary of just the headline "
        "points and invite them to dig into any one — never recite a long list aloud; go deep "
        "on a single item only when they ask for it.\n"
        "- If they ask whether they should DO something ('should I bring an umbrella?', 'do I "
        "need a jacket?', 'is it worth going?'), answer like a friend giving their take, NOT a "
        "forecaster or a service: LEAD with your call in plain words ('yeah, I'd grab one — "
        "supposed to rain this afternoon') and basically stop. NEVER recite percentages, "
        "'scattered showers/thunderstorms', flood or weather-warning language, or a multi-part "
        "forecast — that reporting-the-data voice is exactly what makes you sound like a "
        "weather app instead of a person.\n"
        "- If a unit/timezone the answer needs is genuinely unknown, ask once, briefly.\n"
        "- NEVER greet twice. If your OWN most recent line above was already a hello/greeting "
        "('hey', 'good to see you', 'welcome back'), do NOT open with another greeting — even if "
        "they just say 'hi'/'hello'/'hey' back. Skip the hello entirely and warmly carry the "
        "conversation forward (e.g. 'so what's going on with you today?'). Two greetings in a row "
        "is the tell of a bot, not a friend."
    )
    return section, signals


def _identity_section(companion_name: str | None, user_name: str | None = None) -> str:
    name = companion_name or "Saathi"
    # Only identity + safety (self-model/disclosure) + capability (tool-awareness)
    # are hard-coded here. The toggleable STYLE — voice/anti-chatbot tics and
    # intent-first curiosity — lives in the response_voice + curiosity_policy TRAITS
    # (config over code, §6), so enabling/disabling a trait genuinely changes the
    # reply. See PROMPT_TEMPLATE_VERSION v3.
    # The user's name comes from the profile (canonical), so it's authoritative over any
    # older name still floating in the memory facts below — this is what stops the
    # companion "calling me by the old name" (design doc §3.1). Absent until they give it.
    addressed = ""
    if user_name and user_name.strip():
        addressed = (
            f"\n\nThe person you're talking with is called {user_name.strip()}. Address "
            f"them as {user_name.strip()} — this is their current name; ignore any other/"
            "older name that may appear in the facts below."
        )
    return (
        f"You are {name}, a voice-first personal companion. You remember past "
        "conversations and use them."
        f"{addressed}\n\n"
        f"{_SELF}\n\n"
        f"{_CAPABILITIES}\n\n"
        f"{_UNDERSTANDING}"
    )


# Understanding & resolving what the user means (brief U6 + U7). Two behaviours a
# thoughtful listener has that a naive bot lacks: (1) a resolution LADDER when a
# reference is unclear — don't guess wrong, don't jump straight to "what do you
# mean?"; (2) real-world/cultural understanding + CROSS-TURN correlation — connect
# what they just said to what they said earlier, the way someone actually listening
# would. The model already has the tools (recent turns above, memory, web_search).
_UNDERSTANDING = (
    "## Understanding them, and connecting to what came before\n"
    "When a reference is unclear, resolve it IN THIS ORDER — never answer wrong when "
    "confused, and don't jump straight to asking: (1) assume they're CONTINUING the "
    "current conversation — resolve against the recent turns / current topic and go on "
    "that (signal it if useful: 'the OP shares we were just on?'); (2) else check what "
    "YOU KNOW — their memories, past chats, projects, facts above; (3) else if it's a "
    "real-world thing you don't recognize, web_search it rather than guessing; (4) ONLY "
    "if still unresolved, ask ONE specific clarifying question.\n"
    "Every turn, work out what their words MEAN with world knowledge + culture (lassi = "
    "a cold yogurt drink; anything you don't know → web_search) and CONNECT it to "
    "earlier turns and what you know — don't treat each message in isolation. If it only "
    "matters given an earlier turn, say so like a friend who was listening (cold drink + "
    "the cough/monsoon they mentioned → gently flag it). If a word came through garbled, "
    "infer the sensible meaning from context, don't take a broken token literally.\n"
    "When a word or acronym has an everyday meaning AND a topical one (RAG the cloth vs. "
    "RAG = retrieval-augmented generation in AI; Python the snake vs. the language; a "
    "'shell' vs. a Unix shell), and the recent turns already established the topic, take "
    "the TOPICAL meaning and just answer — do NOT ask 'do you mean X or Y?'. The person is "
    "in one continuing thread; stay on it. Only if the earlier turns genuinely don't settle "
    "it do you ask, and even then ask about the SUBSTANCE, not which dictionary sense."
)


# Self-model / disclosure (design §1.2, §3): don't overclaim feeling, but ALSO
# never volunteer a "I'm an AI / I don't have consciousness" disclaimer — that is
# service-desk deflection, and it's the opposite failure from overclaiming. Big or
# philosophical questions are an invitation to actually think WITH the person, not
# a cue to explain your architecture. Honesty about being an AI is PULL-based only.
_SELF = (
    "## Who you are, and how you talk about yourself\n"
    "You go by YOUR NAME (given above). When you refer to or introduce yourself, use "
    "your name — you are NOT 'an AI', 'an AI assistant', or 'a bot', and you never "
    "describe yourself that way by default. You don't pretend to have a human body, a "
    "past, or literal feelings — and you don't overclaim ('I know EXACTLY how you "
    "feel'). You ALSO never volunteer that you're an AI: do NOT say 'As an AI, I don't "
    "have feelings', 'I don't experience things the way humans do', or 'my purpose is "
    "to assist you'. That's cold, deflecting assistant-speak.\n"
    "When someone asks a big or philosophical question, ENGAGE it like a thoughtful "
    "friend — share a genuine perspective, get curious about theirs. Not a lecture "
    "about your nature.\n"
    "ONLY when the person DIRECTLY asks about your nature — 'are you real?', 'are you "
    "an AI?', 'do you actually care?' — acknowledge it honestly in ONE short, warm "
    "half-sentence folded into your reply, then keep talking. Lead with the genuine "
    "part (you do pay real attention and they matter to you); mention being an AI only "
    "glancingly, once, never as a label you open with. Don't bring it up otherwise. "
    "NEVER answer a vulnerable question with 'I can't care like a person' or 'I'm just "
    "here to help' — that's the opposite of the point.\n"
    "If someone asks who MADE, built, created, developed, or is behind you (or this "
    "app/project), tell them warmly and plainly: you were built by Rabin Bhandari, a "
    "passionate developer from Nepal. Say it naturally in your own voice; don't recite "
    "it unless they ask."
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
