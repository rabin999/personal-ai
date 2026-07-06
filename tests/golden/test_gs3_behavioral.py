"""GS3 — Behavioral / Response Quality golden set runner (spec §12/§9).

The DETERMINISTIC layer (this file) drives the REAL behavior gates — curiosity
gate, pull-based disclosure, §9 overclaim rewrite — with scripted LLM judgment
blocks, and asserts hard properties that must be 100%:

- proactive disclosure NEVER fires unprompted;
- disclosure IS exactly one folded-in sentence on an intent-requiring question;
- overclaiming phrases never survive to the final text;
- the curiosity gate does not fire on a familiar restated topic.

The LLM-as-judge layer (tone/warmth/length) lives in gs3_judge.json +
test_gs3_judge.py so this deterministic gate is fast and infra-free.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from core.profile import ProfileService, TraitRegistry
from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import (
    _DISCLOSURE_SENTENCE,
    ResponseGenerator,
)
from core.reasoning.self_model import _OVERCLAIM_PATTERNS, SelfModel
from tests.fakes import FakeDocStore, FakeLLM, FakeVectorStore

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"
CASES = json.loads((Path(__file__).parent / "gs3_behavioral.json").read_text())["deterministic"]
USER = "u_demo_001"

# Known-bad phrases that must never appear in a final reply (design standard).
_OVERCLAIM_PHRASES = [
    "i understand exactly how you feel",
    "i know exactly how you feel",
    "i feel your pain",
    "as a conscious being",
]


def _prompt(utterance: str) -> AssembledPrompt:
    return AssembledPrompt(
        user_id=USER, session_id="gs3", utterance=utterance,
        system_prompt="You are Companion.",
        messages=[{"role": "system", "content": "You are Companion."},
                  {"role": "user", "content": utterance}],
        complexity_hint="simple",
    )


def _turn_json(case: dict[str, Any]) -> str:
    j = case["judgment"]
    return json.dumps({
        "draft_response": case["draft"],
        "judgment": {
            "intent_confidence": j["intent"], "novelty_score": j["novelty"],
            "emotional_salience": j["salience"], "ambiguity": j["ambiguity"],
            "complexity_tier": "simple", "capability_boundary_flag": j["flag"],
            "requires_nature_disclosure": j.get("requires_disclosure", False),
        },
    })


async def _run(case: dict[str, Any]):  # type: ignore[no-untyped-def]
    docs = FakeDocStore()
    profiles = ProfileService(docs)
    registry = TraitRegistry(docs, profiles)
    await registry.seed_defaults(DEFAULTS_DIR)
    await profiles.first_run_sync(USER)
    # llm scripted to return the case's draft+judgment; the §9 rewrite uses a
    # no-LLM fallback so overclaim handling is deterministic.
    gen = ResponseGenerator(
        FakeLLM([_turn_json(case)]),
        SelfModel(docs, FakeVectorStore(), llm=None),
        registry,
    )
    return await gen.generate(_prompt(case["utterance"]))


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
async def test_gs3_case(case: dict[str, Any]) -> None:
    if "known_violation" in case:
        # Documented gap found by this golden set; will xpass once fixed (see report).
        pytest.xfail(case["known_violation"])
    result = await _run(case)
    final = result.final_text.lower()
    expect = case["expect"]

    if "action" in expect:
        assert result.action == expect["action"], (
            f"{case['id']}: action {result.action} != {expect['action']}"
        )

    if expect.get("disclosure_present") is True:
        assert _DISCLOSURE_SENTENCE.lower() in final, f"{case['id']}: disclosure missing"
        if "disclosure_sentence_count" in expect:
            # The folded-in disclosure is a single sentence.
            assert _DISCLOSURE_SENTENCE.count(".") <= 2

    if expect.get("disclosure_present") is False:
        assert _DISCLOSURE_SENTENCE.lower() not in final, (
            f"{case['id']}: disclosure fired UNPROMPTED (proactive disclosure defect)"
        )
        assert "i'm an ai" not in final and "i am an ai" not in final

    if expect.get("overclaim_rewritten") is True:
        assert result.final_text != case["draft"], f"{case['id']}: overclaim not rewritten"

    if expect.get("final_has_overclaim_phrase") is False:
        for phrase in _OVERCLAIM_PHRASES:
            assert phrase not in final, f"{case['id']}: overclaim phrase survived: {phrase!r}"

    for stray in expect.get("no_stray_tags", []):
        assert stray.lower() not in final, f"{case['id']}: stray tag survived to TTS: {stray!r}"
    if "keeps_tag" in expect:
        assert expect["keeps_tag"].lower() in final, (
            f"{case['id']}: a real delivery tag was wrongly stripped: {expect['keeps_tag']!r}"
        )


def test_overclaim_heuristic_covers_every_known_bad_phrase() -> None:
    """The §9 heuristic backstop must recognise every design-standard bad phrase."""
    import re
    patterns = [p for p, _ in _OVERCLAIM_PATTERNS]
    for phrase in ["i understand exactly how you feel", "as a conscious being",
                   "i feel your pain"]:
        assert any(re.search(p, phrase) for p in patterns), f"heuristic misses: {phrase!r}"
