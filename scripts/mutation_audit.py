"""Mutation audit (engine test session, E0).

A test that cannot go red when the code it covers is broken is not a test. This
harness proves — or disproves — that, one mutation at a time:

    1. apply a single textual mutation to a source file (break one behaviour),
    2. run the engine test subset,
    3. record which tests went red,
    4. restore the file.

A mutation that kills no test names a claim nothing verifies. A test never killed
by any mutation is a candidate for deletion.

Usage:
    uv run python -m scripts.mutation_audit                # run all mutations
    uv run python -m scripts.mutation_audit --only vol_always_false
    uv run python -m scripts.mutation_audit --list
"""
# The `old` anchors below are VERBATIM excerpts of engine source. Reflowing them to
# satisfy the line limit would stop them matching, so long lines are expected here.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "quality" / "mutation_audit.json"

# The engine test subset: reasoning, memory, tools, orchestration, style.
# Voice transducers (VAD/STT/TTS/barge-in) are out of scope for this session.
ENGINE_TESTS = [
    "tests/unit/test_response_gen.py",
    "tests/unit/test_prompt_assembly.py",
    "tests/unit/test_volatility.py",
    "tests/unit/test_dispatcher.py",
    "tests/unit/test_entities.py",
    "tests/unit/test_episodic.py",
    "tests/unit/test_semantic.py",
    "tests/unit/test_procedural.py",
    "tests/unit/test_memory_routing.py",
    "tests/unit/test_recall_routing.py",
    "tests/unit/test_extraction.py",
    "tests/unit/test_self_model.py",
    "tests/unit/test_persona.py",
    "tests/unit/test_psych_model.py",
    "tests/unit/test_tool_leak.py",
    "tests/unit/test_tool_results.py",
    "tests/unit/test_steps.py",
    "tests/unit/test_localtime.py",
    "tests/unit/test_prompt_cache.py",
    "tests/unit/test_llm_router.py",
    "tests/unit/test_model_selection.py",
    "tests/unit/test_audit_fixes.py",
    "tests/unit/test_web_search.py",
    "tests/unit/test_turn_evaluator.py",
    "tests/unit/test_prosody.py",
    "tests/unit/test_turn_error_handling.py",
    "tests/unit/test_projects.py",
    "tests/unit/test_trace_totals.py",
    "tests/unit/test_working_memory.py",
    "tests/golden/test_gs1_memory.py",
    "tests/golden/test_gs2_entities.py",
    "tests/golden/test_gs3_behavioral.py",
    "tests/golden/test_gs3_style.py",
    "tests/golden/test_gs4_learning.py",
    "tests/golden/test_gs5_isolation.py",
    "tests/golden/test_style_judge_agreement.py",
    "tests/engine/test_e1_steps.py",
    "tests/engine/test_e1_reference_spans.py",
    "tests/engine/test_e1_emotional_tool_gate.py",
    "tests/engine/test_e2_volatility_classifier.py",
    "tests/engine/test_e2_detector_agreement.py",
    "tests/engine/test_e1_enforcement.py",
    "tests/engine/test_e3_prosody_read.py",
    "tests/engine/test_e3_now_section.py",
    "tests/engine/test_e5_caller_independence.py",
]


@dataclass(frozen=True)
class Mutation:
    """One surgical break in the engine, and the behaviour it destroys."""

    name: str
    file: str
    old: str
    new: str
    breaks: str  # the engine claim this mutation falsifies
    # Some mutations only bite on the real-call path (a real model, real stores).
    tests: list[str] = field(default_factory=lambda: list(ENGINE_TESTS))


MUTATIONS: list[Mutation] = [
    # ── The two mutations `docs/TEST_AUDIT.md` §6 names as missing. Multi-tenant isolation is
    # a HARD invariant (§0.5), and a silent wrong-resolution is a critical failure; neither was
    # mutation-proven. If either of these survives, that is a defect more serious than anything
    # in docs/DEFECTS_FOUND.md — it means the test guarding the invariant cannot see it break.
    # NOTE on the first: removing only `query_filter=` does NOT leak — the two prefetch legs
    # carry their own `filter=user_filter`, so the post-fusion filter is defence in depth. A
    # mutation that removes it therefore SURVIVES, and would have been reported as a hole in
    # the isolation test rather than as a redundant line of code. The real invariant is that
    # `user_filter` exists at all, so that is what this breaks.
    Mutation(
        name="the_qdrant_search_is_not_user_scoped",
        file="adapters/vector/qdrant.py",
        old="        user_filter = models.Filter(\n            must=[models.FieldCondition(key=USER_ID_FIELD, match=models.MatchValue(value=user_id))]\n        )\n        # Both legs fetch a wider candidate set, filtered per-leg; RRF fuses",
        new="        user_filter = None\n        # Both legs fetch a wider candidate set, filtered per-leg; RRF fuses",
        breaks="§0.5 MULTI-TENANT ISOLATION: every user's vectors become searchable by every other",
    ),
    # And on the second: `reversed(hits)` is a NO-OP for every case in gs2_entities.json,
    # because a dominant reference yields exactly one candidate above `MIN_RESOLUTION_SCORE`.
    # Reversing a one-element list changes nothing. The claim that needed proving was that
    # `resolve()` returns candidates in descending score order, which nothing asserted — and
    # `is_ambiguous()` reads `candidates[0]` and `candidates[1]` positionally.
    Mutation(
        name="entity_resolution_ignores_the_score_order",
        file="core/memory/entities.py",
        old="        return sorted(candidates, key=lambda c: c.score, reverse=True)",
        new="        return sorted(candidates, key=lambda c: c.score)",
        breaks="a silent WRONG resolution: the runner-up is returned as the match",
    ),
    Mutation(
        name="vol_always_false",
        file="core/reasoning/volatility.py",
        old='    text = (utterance or "").strip()\n    if not text:\n        return False',
        new='    text = (utterance or "").strip()\n    return False\n    if not text:\n        return False',
        breaks="the deterministic volatility backstop never fires — a stale answer ships",
    ),
    Mutation(
        name="live_lookup_always_false",
        file="core/reasoning/response_gen.py",
        old="    if prompt.needs_live_info is True:\n        return True",
        new="    if prompt.needs_live_info is True:\n        return False",
        breaks="a turn the reasoning step judged volatile takes the non-agentic path",
    ),
    Mutation(
        name="detector_never_flags",
        file="core/reasoning/style.py",
        old="def find_forbidden(",
        new="def find_forbidden(*_a, **_k):\n    return []\n\n\ndef _dead_find_forbidden(",
        breaks="the style detector flags nothing — self-reflection never triggers",
    ),
    Mutation(
        name="detector_ignores_register",
        file="core/reasoning/style.py",
        old="    (re.compile(p, re.IGNORECASE), label) for p, label in (*FORBIDDEN_PATTERNS, *REGISTER_PATTERNS)",
        new="    (re.compile(p, re.IGNORECASE), label) for p, label in FORBIDDEN_PATTERNS",
        breaks="D-12: the detector reverts to a closed list of remembered phrasings; held-out recall collapses",
    ),
    Mutation(
        name="detector_ignores_the_lead_sentence",
        file="core/reasoning/style.py",
        old="    labels += [label for pattern, label in _COMPILED_LEAD if pattern.search(lead)]",
        new="    labels += []",
        breaks="D-12: an opening service-desk apology ('I'm sorry, I couldn't find that') is not flagged",
    ),
    Mutation(
        name="resolve_the_whole_utterance",
        file="core/memory/entities.py",
        old="def reference_spans(utterance: str) -> list[str]:",
        new="def reference_spans(utterance: str) -> list[str]:\n    return [utterance]",
        breaks="D-13: entity resolution embeds the whole sentence again; an adversarial probe halts the turn",
    ),
    Mutation(
        name="possessive_span_runs_past_the_preposition",
        file="core/memory/entities.py",
        old='        if word.lower().strip(",.;:!?") in _PHRASE_STOP:\n            break',
        new="        if False:\n            break",
        breaks="D-13: 'my portfolio with my brother' becomes one span and resolves to three entities",
    ),
    Mutation(
        name="reflection_never_runs",
        file="core/reasoning/response_gen.py",
        old="        if self._self_reflect and flags_before:",
        new="        if False and self._self_reflect and flags_before:",
        breaks="self-reflection never revises a flagged draft",
    ),
    Mutation(
        name="the_now_section_hands_the_model_a_worked_example",
        file="core/reasoning/prompt_assembly.py",
        old='        "time and day in plain spoken language — never a UTC offset, and never deflect."',
        new="        \"time and day in a natural human way (e.g. 'just past midnight'), never a UTC offset.\"",
        breaks="D-17: the model speaks the prompt's example phrase as the answer",
    ),
    Mutation(
        name="the_model_does_the_timezone_arithmetic",
        file="core/reasoning/localtime.py",
        old="        offset = int((there.utcoffset() or timedelta()).total_seconds() // 60)",
        new="        offset = user_offset",
        breaks="D-17: every place reads 'same time as you'; the offset is wrong in magnitude and direction",
    ),
    Mutation(
        name="the_echo_stripper_eats_the_answer",
        file="core/reasoning/response_gen.py",
        old='    echo = re.compile(\n        rf"(?:(?<=^)|(?<=[.!?…])\\s+){re.escape(q)}\\s*(?=[A-Z(\\"\']|$)",\n        re.IGNORECASE,\n    )',
        new="    echo = re.compile(re.escape(q), re.IGNORECASE)",
        breaks="D-18: the query is cut out of the correct answer — 'The is Balendra Shah!'",
    ),
    Mutation(
        name="an_emotional_turn_still_searches",
        file="core/reasoning/response_gen.py",
        old="    if prompt.needs_live_info is False and _is_emotionally_heavy(prompt):\n        return False",
        new="    if False:\n        return False",
        breaks="D-14: the regex backstop overrides the classifier and searches on a bereavement turn",
    ),
    Mutation(
        name="external_tools_are_always_offered",
        file="core/reasoning/response_gen.py",
        old="    if not _is_emotionally_heavy(prompt) or _requires_live_lookup(prompt):\n        return tools\n    return [t for t in tools if t.id not in _EXTERNAL_WORLD_TOOLS]",
        new="    return tools",
        breaks="D-14: the agentic loop can still request web_search at a grieving user",
    ),
    Mutation(
        name="the_fallback_answers_a_volatile_turn_from_training_data",
        file="core/reasoning/response_gen.py",
        old="        if can_search:\n            repaired = await self._capability_repair(prompt, dispatcher, context)  # type: ignore[arg-type]",
        new="        if False:\n            repaired = await self._capability_repair(prompt, dispatcher, context)  # type: ignore[arg-type]",
        breaks="a judgment-JSON glitch on a volatile turn ships a stale answer with zero searches",
    ),
    Mutation(
        name="empty_is_an_emotion_again",
        file="core/reasoning/prosody.py",
        old='    if text.lower().strip(" .\\"\'") in _NEUTRAL_READS:\n        return None',
        new="    if not text:\n        return None",
        breaks="D-5: the literal word 'empty' parses as sadness; every neutral turn is delivered 'down'",
    ),
    Mutation(
        name="pain_is_not_an_emotion",
        file="core/reasoning/prosody.py",
        old='            r"pain|ache|aching|anguish|distress|sorrow|devastat|bereav|miss (?:him|her|them)",',
        new='            r"zzzznevermatch",',
        breaks="D-5: 'pain' — the design doc's own worked example — yields no emotional read",
    ),
    Mutation(
        name="simple_turns_skip_the_classifier",
        file="adapters/orchestrator/langgraph_orchestrator.py",
        old='        return {"resolution": await self._resolve_note(state["prompt"])}',
        new='        if state["prompt"].complexity_hint == "simple":\n            return {"resolution": _Resolution()}\n        return {"resolution": await self._resolve_note(state["prompt"])}',
        breaks="D-2: generate() forms no volatility verdict on a simple turn; the callers diverge",
    ),
    Mutation(
        name="fallback_skips_the_gates",
        file="core/reasoning/response_gen.py",
        old='        self._span("reasoning", node="fallback_reply", gated=True)',
        new='        return await self._finish(prompt, text, "respond", None)',
        breaks="D-6: a fallback reply skips self-reflection, check_boundary and the curiosity gate",
    ),
    Mutation(
        name="enforcement_is_advisory",
        file="core/reasoning/response_gen.py",
        old="        return scrubbed if salvaged else _SAFE_FALLBACK_TEXT",
        new="        return text",
        breaks="D-7: a draft the engine flagged as assistant-speak ships as the final reply",
    ),
    Mutation(
        name="the_ack_can_be_the_final_reply",
        file="core/reasoning/response_gen.py",
        old="        if owes_an_answer and is_bare_acknowledgement(text, allow_disclosure=allow_disclosure):",
        new="        if False and is_bare_acknowledgement(text, allow_disclosure=allow_disclosure):",
        breaks="D-8/D-16: 'I'll grab that for you right away' ships as the answer to a price question",
    ),
    Mutation(
        name="promise_verbs_are_a_closed_list",
        file="core/reasoning/style.py",
        old="def _is_promise(sentence: str) -> bool:",
        new="def _is_promise(sentence: str) -> bool:\n    return False",
        breaks="D-16: an acknowledgement is not recognised as one, so it survives enforcement",
    ),
    Mutation(
        name="the_streamed_reply_is_never_enforced",
        file="core/reasoning/response_gen.py",
        old="        return self._enforce(prompt, text, allow_disclosure=allow_disc), action",
        new="        return text, action",
        breaks="D-7: the voice path speaks the draft before enforcement can replace it",
    ),
    Mutation(
        name="style_flags_never_reported",
        file="core/reasoning/response_gen.py",
        old="        style_flags = find_forbidden(clean_text, allow_disclosure=allow_disc)",
        new="        style_flags = []",
        breaks="the final result never carries style_flags — enforcement is blind",
    ),
    Mutation(
        name="capability_repair_disabled",
        file="core/reasoning/response_gen.py",
        old='        if not any(t.id == "web_search" for t in dispatcher.tools_for(context)):\n            return None',
        new="        return None",
        breaks="the forced-search backstop never runs — a refusal ships as the answer",
    ),
    Mutation(
        name="search_query_is_raw_utterance",
        file="core/reasoning/response_gen.py",
        old="        if not entities and not user_context:\n            return fallback  # nothing to disambiguate with",
        new="        return prompt.utterance",
        breaks="the search query is not built from the resolved entity — 'OP' hits the crypto token",
    ),
    Mutation(
        name="curiosity_gate_always_responds",
        file="core/reasoning/response_gen.py",
        old='        if judgment.intent_confidence < params["T_intent"]:\n            return "clarify"',
        new='        if False:\n            return "clarify"',
        breaks="the curiosity gate never clarifies on low intent confidence",
    ),
    Mutation(
        name="boundary_never_flags",
        file="core/reasoning/self_model.py",
        old="    async def check_boundary(",
        new="    async def check_boundary(self, *_a, **_k):\n        return BoundaryCheck(flagged=False)\n\n    async def _dead_check_boundary(",
        breaks="the overclaim rewrite never fires — 'I understand exactly how you feel' ships",
    ),
    Mutation(
        name="warm_disclosure_disabled",
        file="core/reasoning/response_gen.py",
        old="        if allow_disc:\n            text = await self._warm_disclosure(prompt, text)",
        new="        if False:\n            text = await self._warm_disclosure(prompt, text)",
        breaks="a nature question gets the cold, unpolished draft",
    ),
    Mutation(
        name="degenerate_rewrite_accepted",
        file="core/reasoning/response_gen.py",
        old="def _is_degenerate_rewrite(original: str, candidate: str) -> bool:",
        new="def _is_degenerate_rewrite(original: str, candidate: str) -> bool:\n    return False",
        breaks="a rewrite that guts the reply to one word is accepted as 'cleaner'",
    ),
    Mutation(
        name="cost_ceiling_never_trips",
        file="core/reasoning/response_gen.py",
        old="    def exceeded(self) -> bool:\n        return self.cap > 0 and self.spent >= self.cap",
        new="    def exceeded(self) -> bool:\n        return False",
        breaks="a runaway tool loop burns the whole budget",
    ),
    Mutation(
        name="tool_leak_not_stripped",
        file="core/reasoning/style.py",
        old="def strip_tool_leak(",
        new="def strip_tool_leak(text, *_a, **_k):\n    return text\n\n\ndef _dead_strip_tool_leak(",
        breaks="'web_search:: …' is spoken out loud",
    ),
    Mutation(
        name="scrub_forbidden_is_identity",
        file="core/reasoning/style.py",
        old="def scrub_forbidden(",
        new="def scrub_forbidden(text, *_a, **_k):\n    return text\n\n\ndef _dead_scrub_forbidden(",
        breaks="the deterministic safety net never removes a banned sentence",
    ),
    Mutation(
        name="register_always_neutral",
        file="core/reasoning/prosody.py",
        old="def read_register(",
        new='def read_register(*_a, **_k):\n    return "neutral"\n\n\ndef _dead_read_register(',
        breaks="the emotional read never selects a register — delivery never varies",
    ),
    Mutation(
        name="query_echo_not_stripped",
        file="core/reasoning/response_gen.py",
        old="def _strip_query_echo(text: str, query: str) -> str:",
        new="def _strip_query_echo(text: str, query: str) -> str:\n    return text",
        breaks="the raw search query is spoken aloud to the user",
    ),
    Mutation(
        name="tag_sanitizer_is_identity",
        file="core/reasoning/response_gen.py",
        old="def _sanitize_tags(text: str) -> str:",
        new="def _sanitize_tags(text: str) -> str:\n    return text",
        breaks="stray bracket tokens reach TTS and the chat UI",
    ),
    Mutation(
        name="judgment_validation_skipped",
        file="core/reasoning/response_gen.py",
        old="class Judgment(BaseModel):",
        new="class Judgment(BaseModel):\n    model_config = {'extra': 'allow'}",
        breaks="(control) a no-op mutation — any test it kills is order-dependent, not behavioural",
    ),
]


def _run(tests: list[str]) -> tuple[set[str], str]:
    """Run the subset; return the set of failing test ids and the raw tail."""
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pytest",
            *tests,
            "-q",
            "-p",
            "no:randomly",
            "--no-header",
            "-x" if False else "--tb=no",
            "-m",
            "not real_call",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    # A parametrized id can contain spaces ("...[who is the current pm?]"), so take
    # the whole id up to pytest's " - <error>" separator, not the first token.
    failed = {
        m.strip()
        for m in re.findall(r"^(?:FAILED|ERROR) (.+?)(?: - |$)", proc.stdout, re.MULTILINE)
    }
    summary = [ln for ln in proc.stdout.splitlines() if re.search(r"\d+ (passed|failed)", ln)]
    return failed, (summary[-1] if summary else "no summary")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", help="run only these mutation names")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for m in MUTATIONS:
            print(f"{m.name:34s} {m.file:38s} {m.breaks}")
        return 0

    todo = [m for m in MUTATIONS if not args.only or m.name in args.only]

    print("=== baseline ===", flush=True)
    t0 = time.time()
    base_failed, base_tail = _run(ENGINE_TESTS)
    print(f"baseline: {base_tail}  ({time.time() - t0:.0f}s)")
    if base_failed:
        print(f"  pre-existing failures (excluded from every mutation): {sorted(base_failed)}")

    results = []
    for m in todo:
        path = ROOT / m.file
        original = path.read_text()
        if m.old not in original:
            print(f"[SKIP] {m.name}: anchor not found in {m.file}", flush=True)
            results.append({"name": m.name, "status": "anchor_not_found", "killed": []})
            continue
        path.write_text(original.replace(m.old, m.new, 1))
        try:
            failed, tail = _run(m.tests)
        finally:
            path.write_text(original)
        killed = sorted(failed - base_failed)
        verdict = "KILLED" if killed else "SURVIVED"
        print(f"[{verdict:8s}] {m.name:34s} {len(killed):3d} test(s)  |  {tail}", flush=True)
        for k in killed[:6]:
            print(f"             ↳ {k}")
        results.append(
            {
                "name": m.name,
                "file": m.file,
                "breaks": m.breaks,
                "status": verdict,
                "killed": killed,
                "killed_count": len(killed),
            }
        )

    # A `--only` run is a spot check, not an audit. Writing it to the audit file would
    # silently replace the full matrix with a single row — which is how the per-file
    # coverage table came out empty the first time it was generated.
    out = OUT if not args.only else OUT.with_name("mutation_audit_partial.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"baseline_failures": sorted(base_failed), "mutations": results}, indent=2)
    )
    print(f"\nwrote {out.relative_to(ROOT)}")
    survived = [r["name"] for r in results if r["status"] == "SURVIVED"]
    if survived:
        print(f"\nSURVIVED (nothing tests these): {survived}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
