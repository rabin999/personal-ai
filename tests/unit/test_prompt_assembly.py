"""Unit tests for Prompt Assembly (spec §10) — real core modules over fake ports."""

from pathlib import Path

import pytest

from core.memory.entities import EntityResolver
from core.memory.episodic import EpisodicMemory
from core.memory.procedural import ProceduralMemory
from core.memory.semantic import SemanticMemory
from core.memory.working import Turn, WorkingMemory
from core.profile import ProfileService, TraitRegistry
from core.reasoning.prompt_assembly import (
    AssembledPrompt,
    DisambiguationRequest,
    PromptAssembler,
)
from core.reasoning.self_model import SelfModel
from ports.graph_store import Fact
from tests.fakes import FakeDocStore, FakeGraphStore, FakeVectorStore

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"
USER = "u_demo_001"
SESSION = "s_test"


class StubProjects:
    def __init__(self, contexts: dict[str, str] | None = None) -> None:
        self.contexts = contexts or {}

    async def project_context(self, user_id: str, entity_id: str) -> str | None:
        return self.contexts.get(entity_id)


class StubPsych:
    def __init__(self, text: str = "") -> None:
        self.text = text

    async def render_for_prompt(self, user_id: str) -> str:
        return self.text


class Harness:
    def __init__(
        self,
        projects: StubProjects | None = None,
        psych: StubPsych | None = None,
        char_budget: int = 24_000,
    ):
        self.docs = FakeDocStore()
        self.vectors = FakeVectorStore()
        self.graph = FakeGraphStore()
        self.working = WorkingMemory()
        self.profiles = ProfileService(self.docs)
        self.registry = TraitRegistry(self.docs, self.profiles)
        self.episodic = EpisodicMemory(self.vectors)
        self.semantic = SemanticMemory(self.graph)
        self.procedural = ProceduralMemory(self.docs)
        self.entities = EntityResolver(self.vectors)
        self.self_model = SelfModel(self.docs, self.vectors)
        self.assembler = PromptAssembler(
            self.profiles,
            self.registry,
            self.working,
            self.episodic,
            self.semantic,
            self.procedural,
            self.entities,
            self.self_model,
            projects=projects,
            psych=psych,
            char_budget=char_budget,
        )

    async def seed(self) -> None:
        await self.registry.seed_defaults(DEFAULTS_DIR)
        await self.profiles.first_run_sync(USER)


@pytest.fixture
async def harness() -> Harness:
    h = Harness()
    await h.seed()
    return h


# Acceptance: a referenced project's ledger appears in the assembled prompt.
async def test_referenced_project_ledger_appears_in_prompt() -> None:
    projects = StubProjects(
        {"proj_nepse": "NEPSE Portfolio ledger: bought 20 SYPNL @ 42 on 2026-07-01"}
    )
    h = Harness(projects=projects)
    await h.seed()
    await h.entities.index(
        USER, "project", "proj_nepse", "NEPSE Portfolio", "stock trading tracker portfolio"
    )

    result = await h.assembler.assemble(USER, SESSION, "how's my NEPSE Portfolio doing?")

    assert isinstance(result, AssembledPrompt)
    assert "bought 20 SYPNL @ 42" in result.system_prompt
    assert result.resolved_entities[0].entity_id == "proj_nepse"


# U6/U7: the reference-resolution ladder + cross-turn correlation guidance is composed
# into every prompt (with the tools present to actually execute the ladder).
async def test_understanding_ladder_and_correlation_in_prompt() -> None:
    h = Harness()
    await h.seed()
    result = await h.assembler.assemble(USER, SESSION, "should I go easy on it?")
    assert isinstance(result, AssembledPrompt)
    sp = result.system_prompt.lower()
    # U6 ladder, in order: assume current context → what you know → web_search → ask.
    assert "in this order" in sp
    i_ctx = sp.find("continuing the current conversation")
    i_know = sp.find("what you know")
    i_search = sp.find("web_search it rather than guessing")
    i_ask = sp.find("clarifying question")
    assert 0 < i_ctx < i_know < i_search < i_ask
    # U7 world-knowledge + cross-turn correlation exemplar.
    assert "lassi" in sp and "connect it to" in sp
    # The prompt template version was bumped so the trace attributes this behavior.
    assert result.prompt_version.startswith("pt5")


# U10/U11/U12: audio-awareness directives reach the prompt when settings are on, and
# are absent when off / not in surroundings mode.
async def test_audio_awareness_directives_gated_by_settings() -> None:
    from core.audio.awareness import HealthMonitor
    from ports.sound import SoundRead

    h = Harness()
    await h.seed()
    # Settings ON: mimic tone + surroundings mode + health check-ins (default on).
    await h.profiles.update(
        USER, {"audio_prefs": {"mimic_tone": True, "ambient_mode": "surroundings"}}
    )
    whisper = SoundRead(vocal_register="whisper", ambient_voices=True)
    checkin = HealthMonitor().observe(SoundRead(health_sounds=["cough"], confidence=0.9))
    result = await h.assembler.assemble(USER, SESSION, "you there?", sound=whisper, health=checkin)
    assert isinstance(result, AssembledPrompt)
    sp = result.system_prompt.lower()
    assert "whisper" in sp  # U11 mirror directive present
    assert result.mirror_register == "whisper"
    assert "another person" in sp  # U12 surroundings awareness
    assert "not transcribing" in sp  # privacy gate note (transcribe_others default off)
    assert "check in" in sp  # U10 caring check-in directive

    # Settings OFF (mimic off, near mode) → none of those directives.
    await h.profiles.update(USER, {"audio_prefs": {"mimic_tone": False, "ambient_mode": "near"}})
    off = await h.assembler.assemble(USER, SESSION, "you there?", sound=whisper, health=checkin)
    assert off.mirror_register is None
    assert "another person" not in off.system_prompt.lower()


# §17 rule 3: soft psychological signals reach the assembled prompt (§17 → §10).
async def test_psych_signals_reach_the_prompt() -> None:
    signal = (
        "Soft signals about this user (probabilistic hints, never certainties):\n"
        "- tends toward higher conscientiousness (tentative, confidence 0.6)"
    )
    h = Harness(psych=StubPsych(signal))
    await h.seed()
    result = await h.assembler.assemble(USER, SESSION, "hey")
    assert isinstance(result, AssembledPrompt)
    assert "tends toward higher conscientiousness" in result.system_prompt
    # An empty psych read adds nothing (fresh user).
    h2 = Harness(psych=StubPsych(""))
    await h2.seed()
    empty = await h2.assembler.assemble(USER, SESSION, "hey")
    assert isinstance(empty, AssembledPrompt)
    assert empty.sections["psych"] == ""


# Acceptance: ambiguous entity → disambiguation request, not a prompt.
async def test_ambiguous_entities_halt_with_disambiguation_request(
    harness: Harness,
) -> None:
    await harness.entities.index(
        USER, "project", "proj_a", "NEPSE tracker", "stock trading tracker portfolio"
    )
    await harness.entities.index(
        USER, "project", "proj_b", "US tracker", "stock trading tracker portfolio"
    )

    result = await harness.assembler.assemble(USER, SESSION, "update my trading tracker")

    assert isinstance(result, DisambiguationRequest)
    assert {c.entity_id for c in result.candidates[:2]} == {"proj_a", "proj_b"}


# Acceptance: over-budget trims episodic before utterance/working memory.
async def test_over_budget_trims_episodic_first_never_utterance_or_recent_turns() -> None:
    # Budget just above the (non-trimmable) persona+traits floor so the 6 big
    # episodic chunks still overflow and must be trimmed first (rule 9). The
    # identity section carries the always-present capability + self-model blocks
    # (~3.8k chars), so the floor sits near 5k — the budget tracks it.
    h = Harness(char_budget=5_000)
    await h.seed()
    harness_turns = [
        Turn(role="user", text="yesterday was rough at work"),
        Turn(role="assistant", text="want to talk about it?"),
    ]
    for turn in harness_turns:
        h.working.append(SESSION, turn)
    await h.episodic.write(
        USER,
        "s_old",
        [f"user: memory chunk about work stress number {i} " + "x" * 400 for i in range(6)],
    )

    result = await h.assembler.assemble(USER, SESSION, "work is stressful again today")

    assert isinstance(result, AssembledPrompt)
    # Non-negotiables survive:
    assert result.messages[-1]["content"] == "work is stressful again today"
    transcript = [m["content"] for m in result.messages]
    assert "yesterday was rough at work" in transcript
    # Episodic gave way: the real acceptance is that the 6 big episodic chunks were
    # trimmed (not all 6 survive), while the persona floor + the non-trimmable
    # capability/self blocks stay. The ceiling sits just above that floor and well
    # below floor + all 6 chunks (~11.9k), proving episodic was dropped to fit.
    # Ceiling tracks the (non-trimmable) pinned floor — identity + capabilities +
    # the U6/U7 understanding block + traits + user-local-time + the delivery rules
    # (which now carry the recommendation + long-list-summary guidance, response
    # standard §12) — and sits below floor + all 6 episodic chunks.
    assert len(result.system_prompt) <= 9_500
    assert result.system_prompt.count("memory chunk") < 6
    # Traits (P1) survived the trim:
    assert "clarifying question" in result.system_prompt


# Acceptance: enabled traits + high-confidence procedural rules included.
async def test_prompt_includes_trait_descriptions_and_promoted_rules(
    harness: Harness,
) -> None:
    rule = await harness.procedural.add_candidate(
        USER,
        rule_text="when user says they need a win, offer one small concrete task",
        trigger="need a win",
        action="offer task",
    )
    for _ in range(5):
        await harness.procedural.reinforce(USER, rule.id)

    result = await harness.assembler.assemble(USER, SESSION, "I really need a win today")

    assert isinstance(result, AssembledPrompt)
    assert "clarifying question" in result.system_prompt  # curiosity trait description
    assert "offer one small concrete task" in result.system_prompt
    # Candidate (below threshold) rules stay out:
    weak = await harness.procedural.add_candidate(
        USER, rule_text="weak rule text marker", trigger="need a win", action="x"
    )
    result2 = await harness.assembler.assemble(USER, SESSION, "I really need a win today")
    assert isinstance(result2, AssembledPrompt)
    assert "weak rule text marker" not in result2.system_prompt
    assert weak.confidence < 0.6


async def test_semantic_facts_carry_validity_markers(harness: Harness) -> None:
    harness.graph.seed_fact(
        USER,
        Fact(fact="the brother of the user is Tom", valid_to="2026-06-01T00:00:00+00:00"),
    )
    harness.graph.seed_fact(USER, Fact(fact="the brother of the user is Max"))

    result = await harness.assembler.assemble(USER, SESSION, "tell my brother something")

    assert isinstance(result, AssembledPrompt)
    assert "the brother of the user is Max" in result.system_prompt
    assert (
        "the brother of the user is Tom [superseded 2026-06-01T00:00:00+00:00]"
        in result.system_prompt
    )


async def test_emotion_signal_and_complexity_hint_travel_with_prompt(
    harness: Harness,
) -> None:
    short = await harness.assembler.assemble(
        USER, SESSION, "hey", emotion={"valence": -0.4, "label": "tired"}
    )
    assert isinstance(short, AssembledPrompt)
    assert short.emotion == {"valence": -0.4, "label": "tired"}
    assert short.complexity_hint == "simple"

    heavy = await harness.assembler.assemble(
        USER,
        SESSION,
        "can you explain the tradeoff and help me decide whether I should i rebalance "
        "my whole portfolio strategy versus keeping cash, and plan the steps?",
    )
    assert isinstance(heavy, AssembledPrompt)
    assert heavy.complexity_hint == "complex"


async def test_unknown_references_resolve_to_nothing_and_do_not_halt(
    harness: Harness,
) -> None:
    result = await harness.assembler.assemble(USER, SESSION, "what's the weather like")
    assert isinstance(result, AssembledPrompt)
    assert result.resolved_entities == []


async def test_recent_turns_render_in_order_before_utterance(harness: Harness) -> None:
    harness.working.append(SESSION, Turn(role="user", text="first message"))
    harness.working.append(SESSION, Turn(role="assistant", text="first reply"))

    result = await harness.assembler.assemble(USER, SESSION, "second message")

    assert isinstance(result, AssembledPrompt)
    roles = [m["role"] for m in result.messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert result.messages[1]["content"] == "first message"


# Acceptance (§10 graceful degradation): a memory store being down drops that
# context layer but the turn's prompt still assembles — no crash.
async def test_assembly_degrades_when_a_memory_store_is_down() -> None:
    h = Harness()
    await h.seed()

    async def boom(*a, **k):
        raise RuntimeError("store down")

    h.episodic.retrieve = boom  # type: ignore[method-assign]
    h.semantic.facts_for = boom  # type: ignore[method-assign]

    result = await h.assembler.assemble(USER, SESSION, "how's it going?")

    assert isinstance(result, AssembledPrompt)
    assert result.system_prompt  # still produced a usable prompt
    assert result.messages[-1]["content"] == "how's it going?"  # utterance preserved
