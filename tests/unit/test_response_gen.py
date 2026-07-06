"""Unit tests for Response Generation & Behavior Gates (spec §12) — LLM scripted."""

import json
from pathlib import Path
from typing import Any

from core.memory.entities import EntityCandidate
from core.profile import ProfileService, TraitRegistry
from core.reasoning.prompt_assembly import AssembledPrompt, DisambiguationRequest
from core.reasoning.response_gen import ResponseGenerator
from core.reasoning.self_model import SELF_MODEL_LOG_COLLECTION, SelfModel
from tests.fakes import FakeDocStore, FakeLLM, FakeVectorStore

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"
USER = "u_demo_001"


def _prompt(utterance: str = "hey, how are you?") -> AssembledPrompt:
    return AssembledPrompt(
        user_id=USER,
        session_id="s1",
        utterance=utterance,
        system_prompt="You are Companion.",
        messages=[
            {"role": "system", "content": "You are Companion."},
            {"role": "user", "content": utterance},
        ],
        complexity_hint="simple",
    )


def _turn_json(
    draft: str = "doing great — how was your day?",
    intent: float = 0.9,
    novelty: float = 0.2,
    salience: float = 0.3,
    ambiguity: float = 0.1,
    flag: str | None = None,
) -> str:
    return json.dumps(
        {
            "draft_response": draft,
            "judgment": {
                "intent_confidence": intent,
                "novelty_score": novelty,
                "emotional_salience": salience,
                "ambiguity": ambiguity,
                "complexity_tier": "simple",
                "capability_boundary_flag": flag,
            },
        }
    )


class Harness:
    def __init__(self, responses: list[Any]) -> None:
        self.docs = FakeDocStore()
        self.vectors = FakeVectorStore()
        self.llm = FakeLLM(responses)
        profiles = ProfileService(self.docs)
        self.registry = TraitRegistry(self.docs, profiles)
        self.self_model = SelfModel(self.docs, self.vectors, self.llm)
        self.generator = ResponseGenerator(self.llm, self.self_model, self.registry)

    async def seed(self) -> "Harness":
        await self.registry.seed_defaults(DEFAULTS_DIR)
        await ProfileService(self.docs).first_run_sync(USER)
        return self


async def _generator(responses: list[Any]) -> Harness:
    return await Harness(responses).seed()


# ── rule 1: dual output validated; retry once; safe fallback ─────────────


async def test_malformed_judgment_retries_once_then_succeeds() -> None:
    h = await _generator(["{not json at all", _turn_json()])
    result = await h.generator.generate(_prompt())
    assert result.action == "respond"
    assert result.final_text.startswith("doing great")
    assert len(h.llm.calls) == 2  # first invalid, one retry


async def test_two_bad_payloads_fall_back_to_safe_clarify() -> None:
    h = await _generator(["nope", "still nope"])
    result = await h.generator.generate(_prompt())
    assert result.action == "clarify"
    assert result.judgment is None
    assert result.final_text  # safe non-empty fallback


# ── rule 2: curiosity gate (acceptance 1-2) ──────────────────────────────


async def test_low_intent_confidence_triggers_clarify_not_a_guess() -> None:
    h = await _generator([_turn_json(draft="I think you mean X?", intent=0.3)])
    result = await h.generator.generate(_prompt("do the thing with the stuff"))
    assert result.action == "clarify"


async def test_familiar_low_novelty_topic_does_not_force_followup() -> None:
    h = await _generator([_turn_json(novelty=0.2, salience=0.9)])
    result = await h.generator.generate(_prompt("work was long again"))
    assert result.action == "respond"


async def test_novel_and_salient_topic_invites_curious_followup() -> None:
    h = await _generator([_turn_json(novelty=0.9, salience=0.8)])
    result = await h.generator.generate(_prompt("I met someone special yesterday"))
    assert result.action == "curious_followup"


async def test_high_ambiguity_alone_is_not_high_stakes(
) -> None:
    h = await _generator([_turn_json(ambiguity=0.9, salience=0.2, intent=0.8)])
    result = await h.generator.generate(_prompt("maybe change it a bit?"))
    assert result.action == "respond"

    h2 = await _generator([_turn_json(ambiguity=0.9, salience=0.9, intent=0.8)])
    result2 = await h2.generator.generate(_prompt("should I confront her about it?"))
    assert result2.action == "clarify"


async def test_gate_defaults_apply_when_trait_params_missing() -> None:
    h = Harness([_turn_json(intent=0.3)])  # no seed → no traits at all
    result = await h.generator.generate(_prompt())
    assert result.action == "respond"  # trait disabled → gate off


# ── rule 3: overclaim rewrite before output (acceptance 4) ───────────────


async def test_flagged_overclaim_is_rewritten_before_leaving_module() -> None:
    h = await _generator(
        [
            _turn_json(
                draft="I understand exactly how you feel.", flag="overclaim_empathy"
            ),
            "That sounds really heavy — I'm here.",  # rewrite call
        ]
    )
    result = await h.generator.generate(_prompt("my dad is sick"))
    assert result.final_text == "That sounds really heavy — I'm here."


# ── rule 4: pull-based disclosure (acceptance 3) ─────────────────────────


async def test_do_you_actually_care_gets_one_sentence_disclosure() -> None:
    h = await _generator([_turn_json(draft="I do care about how you're doing.")])
    result = await h.generator.generate(_prompt("do you actually care about me?"))
    assert "I'm an AI" in result.final_text
    assert result.final_text.count("AI") == 1  # folded in once, not a lecture


async def test_ordinary_chat_never_volunteers_disclosure() -> None:
    h = await _generator([_turn_json()])
    result = await h.generator.generate(_prompt("what should I cook tonight?"))
    assert "AI" not in result.final_text


# ── disambiguation handoff ───────────────────────────────────────────────


async def test_disambiguation_request_produces_question_without_llm_call() -> None:
    h = await _generator([])
    request = DisambiguationRequest(
        user_id=USER,
        session_id="s1",
        utterance="my tracker",
        candidates=[
            EntityCandidate(entity_id="a", entity_type="project", name="NEPSE Tracker", score=2),
            EntityCandidate(entity_id="b", entity_type="project", name="US Tracker", score=2),
        ],
    )
    result = await h.generator.generate(request)
    assert result.action == "disambiguate"
    assert "NEPSE Tracker" in result.final_text and "US Tracker" in result.final_text
    assert h.llm.calls == []


# ── rule 6: every turn logs to the self-model (acceptance §9-3) ──────────


async def test_every_turn_writes_one_self_model_log_entry() -> None:
    h = await _generator([_turn_json()])
    result = await h.generator.generate(_prompt())
    rows = await h.docs.find(SELF_MODEL_LOG_COLLECTION)
    assert len(rows) == 1
    assert rows[0]["_id"] == result.turn_id
    assert rows[0]["user_id"] == USER


async def test_emotion_signal_reaches_the_llm_prompt() -> None:
    h = await _generator([_turn_json()])
    prompt = _prompt("rough day")
    prompt.emotion = {"label": "sad", "valence": -0.6}
    await h.generator.generate(prompt)
    joined = json.dumps(h.llm.calls[0]["messages"])
    assert "sad" in joined


async def test_intent_signal_fires_disclosure_when_regex_misses() -> None:
    # "are you a bot" is NOT in the regex backstop; the judgment intent flag drives it.
    h = Harness([_turn_json(draft="Nah, not a person — what were you saying?")
                 .replace('"complexity_tier": "simple"',
                          '"complexity_tier": "simple", "requires_nature_disclosure": true')])
    result = await h.generator.generate(_prompt("are you a bot?"))
    assert "i'm an ai" in result.final_text.lower()


async def test_no_disclosure_without_intent_or_regex() -> None:
    h = Harness([_turn_json(draft="Quick pasta sounds great.")])
    result = await h.generator.generate(_prompt("what should I cook tonight?"))
    assert "i'm an ai" not in result.final_text.lower()
