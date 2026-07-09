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
    "tests/engine/test_e2_volatility_classifier.py",
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
        name="reflection_never_runs",
        file="core/reasoning/response_gen.py",
        old="        if self._self_reflect and flags_before:",
        new="        if False and self._self_reflect and flags_before:",
        breaks="self-reflection never revises a flagged draft",
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
        old='def _strip_query_echo(text: str, query: str) -> str:\n    """Remove a verbatim echo of the search query from a spoken draft."""',
        new='def _strip_query_echo(text: str, query: str) -> str:\n    """Remove a verbatim echo of the search query from a spoken draft."""\n    return text',
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
