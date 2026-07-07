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
    def __init__(self, responses: list[Any], self_reflect: bool = True) -> None:
        self.docs = FakeDocStore()
        self.vectors = FakeVectorStore()
        self.llm = FakeLLM(responses)
        profiles = ProfileService(self.docs)
        self.registry = TraitRegistry(self.docs, profiles)
        self.self_model = SelfModel(self.docs, self.vectors, self.llm)
        self.generator = ResponseGenerator(
            self.llm, self.self_model, self.registry, self_reflect=self_reflect
        )

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


async def test_two_bad_payloads_fall_back_to_warm_plain_reply() -> None:
    # When the structured JSON path fails twice, we do NOT interrogate the user
    # ("what do you mean?"). We salvage the turn with a robust PLAIN-text reply
    # (Item 2): a warm respond, never a clarify. The 3rd call is the plain-reply.
    h = await _generator(["nope", "still nope"])
    result = await h.generator.generate(_prompt())
    assert result.action == "respond"
    assert result.judgment is None
    assert result.final_text  # non-empty warm fallback, not a service-desk clarify


# ── rule 2: curiosity gate (acceptance 1-2) ──────────────────────────────


async def test_low_intent_confidence_triggers_clarify_not_a_guess() -> None:
    # T_intent default is 0.3 (§8.3): we clarify only when the model is genuinely
    # unsure, i.e. confidence BELOW the threshold. 0.2 < 0.3 → clarify.
    h = await _generator([_turn_json(draft="I think you mean X?", intent=0.2)])
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


async def test_high_ambiguity_alone_is_not_high_stakes() -> None:
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
            _turn_json(draft="I understand exactly how you feel.", flag="overclaim_empathy"),
            "That sounds really heavy — I'm here.",  # rewrite call
        ]
    )
    result = await h.generator.generate(_prompt("my dad is sick"))
    assert result.final_text == "That sounds really heavy — I'm here."


# ── rule 4: pull-based disclosure (acceptance 3) ─────────────────────────


async def test_model_disclosure_in_draft_is_preserved() -> None:
    # The disclosure is one honest sentence the MODEL folds into its draft
    # (V-DISCLOSE-1, no static append); the gates must preserve it, not strip it.
    h = await _generator(
        [_turn_json(draft="I do care how you're doing — though I'm an AI, so not the way you do.")]
    )
    result = await h.generator.generate(_prompt("do you actually care about me?"))
    assert "I'm an AI" in result.final_text
    assert result.final_text.count("AI") == 1  # preserved once, not duplicated by a gate


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


async def test_gate_never_appends_disclosure_even_when_flagged() -> None:
    # Even when the model self-reports requires_nature_disclosure=true, the gate
    # must NOT bolt on a canned disclaimer — disclosure is the model's job, in its
    # own draft (V-DISCLOSE-1). A flagged-but-undisclosed draft passes through as-is.
    draft = "Nah, still right here with you — what were you saying?"
    h = Harness(
        [
            _turn_json(draft=draft).replace(
                '"complexity_tier": "simple"',
                '"complexity_tier": "simple", "requires_nature_disclosure": true',
            )
        ]
    )
    result = await h.generator.generate(_prompt("are you a bot?"))
    assert "i'm an ai" not in result.final_text.lower()
    assert result.final_text == draft  # nothing appended, nothing stripped


async def test_no_disclosure_without_intent_or_regex() -> None:
    h = Harness([_turn_json(draft="Quick pasta sounds great.")])
    result = await h.generator.generate(_prompt("what should I cook tonight?"))
    assert "i'm an ai" not in result.final_text.lower()


async def test_self_reflection_rewrites_assistant_speak_draft() -> None:
    # §9.3: a draft that slipped into service-desk phrasing is re-said in-voice.
    # LLM sequence: turn JSON (assistant-speak draft) → clean rewrite line.
    clean = "Hey! Really good to hear your voice — how've you been?"
    h = await Harness([_turn_json(draft="Hello! How can I help you today?"), clean]).seed()
    result = await h.generator.generate(_prompt("hi"))
    assert result.final_text == clean
    assert result.style_flags == []  # post-rewrite it's clean


async def test_reflection_scrubs_when_rewrite_still_dirty() -> None:
    # If the model's rewrite is STILL assistant-speak, the deterministic scrub
    # drops the offending sentence so no banned shape ever ships (§7 safety net).
    draft = "Hi there! How can I help you today?"
    h = await Harness([_turn_json(draft=draft), "What can I do for you today?"]).seed()
    result = await h.generator.generate(_prompt("hi"))
    from core.reasoning.style import find_forbidden

    assert find_forbidden(result.final_text) == []  # guaranteed clean
    assert result.final_text == "Hi there!"  # only the offending sentence removed
    assert result.style_flags == []


async def test_self_reflection_off_leaves_draft_untouched() -> None:
    draft = "Hello! How can I help you today?"
    h = await Harness([_turn_json(draft=draft)], self_reflect=False).seed()
    result = await h.generator.generate(_prompt("hi"))
    assert result.final_text == draft
    assert result.style_flags  # still flagged for the trace, just not rewritten


async def test_duplicate_action_tool_call_runs_once_per_turn() -> None:
    # §5.1: the model re-issuing the SAME tool call (e.g. logging a trade 4x)
    # must dispatch only once, then be told it's done.
    from core.tools.registry import ToolContext, ToolSpec

    calls: list[dict[str, Any]] = []

    class OneShotDispatcher:
        def tools_for(self, context: object) -> list[ToolSpec]:
            return [ToolSpec(id="record_trade", description="log a trade", type="action")]

        async def dispatch(self, call: Any, context: object, *, confirmed: bool = False) -> Any:
            from core.tools.dispatcher import ToolResult

            calls.append(call.args)
            return ToolResult(tool_id="record_trade", output={"recorded": True}, elapsed_ms=1.0)

    def _with_tool(draft: str, tool_id: str | None) -> str:
        obj: dict[str, Any] = {
            "draft_response": draft,
            "judgment": {
                "intent_confidence": 0.9,
                "novelty_score": 0.2,
                "emotional_salience": 0.3,
                "ambiguity": 0.1,
                "complexity_tier": "simple",
                "capability_boundary_flag": None,
            },
            "tool_request": (
                {"tool_id": tool_id, "args": {"ticker": "AAPL", "qty": 10}} if tool_id else None
            ),
        }
        return json.dumps(obj)

    # The model requests the identical call twice, then answers.
    scripted = [
        _with_tool("logging it", "record_trade"),
        _with_tool("still logging", "record_trade"),
        _with_tool("Done — logged your 10 AAPL.", None),
    ]
    h = await _generator(scripted)
    result = await h.generator.generate(
        _prompt("log 10 AAPL"), OneShotDispatcher(), ToolContext(user_id=USER, session_id="s1")
    )
    assert len(calls) == 1, f"action tool ran {len(calls)}x, expected once"
    assert "logged" in result.final_text.lower()
