# Design-doc coverage matrix

**Purpose.** One place that answers: *for each requirement in the design doc / spec, is it
actually evaluated — and can that evaluation fail?* This is the map from "the design says X"
to "here is the instrument that proves X, and here is the mutation that turns it red."

It exists because this repo has repeatedly found **vacuous** metrics — a detector that flagged
nothing scored 1.000; an isolation test that asserted a string absent from an *invented*
sentence passed for the wrong reason; a disambiguation guard that "passed" because turns never
reached the engine (see `docs/DEFECTS_FOUND.md`, `docs/TEST_AUDIT.md`). A green number is a
*claim*; a claim that cannot fail is not evidence.

> Scope note: this matrix is authored during the **core-engine** work. Voice I/O rows
> (§19–24) are listed for completeness but marked ⬜ **out of current scope** — not being
> worked on or re-verified in this pass. The **verified-retrieval** rows are 🔵 **in progress**
> (built on a separate branch).

## The bar: what makes an evaluation *real* (every ✅ must clear all five)

1. **Ground truth exists** — a human-labeled set, not the model grading itself in a circle.
2. **Asserted on the trace, not the reply string** — "did it search / reflect / write memory",
   read from spans. A reply-only `assert "x" not in reply` passes trivially on a broken engine.
3. **A killing mutation exists** — break the behaviour, a test goes red (`scripts/mutation_audit.py`).
4. **Measured out-of-sample** — held-out set, frozen separately from the fix.
5. **Reported per-source, not pooled** — pooling is what hid the 0.000 recall (D-12).

## Legend

| | meaning |
|---|---|
| ✅ | **Proven** — real evaluation + a killing mutation; clears the bar above |
| 🟡 | **Partial** — mechanism tested, but accuracy/quality not measured on a labeled/held-out set, or no mutation |
| 🔴 | **Unmeasured** — no instrument today |
| 🔵 | **In progress** — being built (verified retrieval, separate branch) |
| ⬜ | **Out of current scope** — voice I/O; not re-verified this pass |

---

## A. Non-negotiable invariants (design §3 / CLAUDE.md §3)

| Requirement | Source | Instrument | Killing mutation | Status |
|---|---|---|---|---|
| Multi-tenant isolation (no cross-user leak) | §3.1, spec §26 | `tests/golden/test_gs5_isolation.py` (trace-level: what retrieval puts in B's prompt) | `the_qdrant_search_is_not_user_scoped` (kills 8) | ✅ |
| `user_id` from resolved context, never hard-coded | §3.2, spec §26 | §26 acceptance (`tests/acceptance`), static-user resolve | — | 🟡 |
| Ports→adapters boundary (`core ↛ adapters`) | §3.3 | `uv run lint-imports` (import-linter, CI) | contract is the check | ✅ |
| Money-costing calls log to Cost Ledger; cache hit = $0 | §3.4 | cost unit/integration tests | `cost_ceiling_never_trips` | 🟡 |
| Every LLM JSON Pydantic-validated (retry→fallback) | §3.5 | `tests/engine/test_e1_enforcement.py` | `judgment_validation_skipped` (control) | 🟡 |
| Config over code (params in config) | §3.6 | — | — | 🔴 |
| Never speaks first | §3.7 | `engine_gate` `restraint_greeting`/`restraint_signoff` | — | 🟡 |
| Idle is nearly free (VAD gate blocks paid calls) | §3.8, spec §19 | `tests/…` VADGate cost-gate | — | ⬜ |
| Never diagnose (signals, not claims) | §3.9, spec §17 | §17 clinical-language tests | — | 🟡 |
| Async-first (slow work → queue) | §3.10 | architectural | — | 🔴 |

## B. The reasoning loop — think, then reflect (design §2 / §9)

| Requirement | Source | Instrument | Killing mutation | Status |
|---|---|---|---|---|
| Reasons every turn before replying (ReAct) | §2 | `engine_gate` reasoning/tool spans (trace) | — | 🟡 |
| **Self-reflection is first-class, every turn** | §2, design §9.3 | `engine_gate` "no reflection span"=0; `test_e1_enforcement.py` | `reflection_never_runs`, `fallback_skips_the_gates`, `enforcement_is_advisory` | ✅ |
| Reads memory before reasoning; assembles context | §2, spec §10 | `tests/unit/test_prompt_assembly.py` (order/budget) | — | 🟡 |
| Curiosity gate (clarify / curious-followup) | spec §12 | `engine_gate` scenarios | `curiosity_gate_always_responds` | 🟡 (accuracy on a labeled set 🔴) |
| Pull-based disclosure (one sentence, never volunteered) | design §1.2, spec §12 | `engine_gate` `nature_disclosure`, `gs3` | `warm_disclosure_disabled` | 🟡 |
| Overclaim rewrite (never "I feel your pain too") | design §1.4, §5.2 | `test_e1_enforcement.py` | `boundary_never_flags`, `degenerate_rewrite_accepted` | ✅ |

## C. Response quality — sound like the companion (design §1 / spec §12)

| Requirement | Source | Instrument | Killing mutation | Status |
|---|---|---|---|---|
| Not assistant/service-desk speak (`chatbot_like`) | §1, spec §12 | `engine_gate` + `core/eval/judge.py`; `scripts/detector_agreement.py` (held-out recall 0.955) | `detector_never_flags`, `detector_ignores_register`, `detector_ignores_the_lead_sentence` | ✅ instrument · gate metric not yet at 0 |
| Concise; long answer → summary → drill-down | §1, response std | `scripts/summarize_probe.py` (real convo, both callers) | — | 🟡 (proven by convo; no mutation yet) |
| Recommendation-not-dump (D-20) | `GOLDEN_SETS_INDIRECT` | `engine_gate` `umbrella` (1/20) | — | 🟡 |
| Informal/casual default | §1, spec §12 | — (chatbot_like is only a proxy) | — | 🔴 |
| Register / prosody selection | design §3.6.5 | `test_e3_prosody_read.py` | `register_always_neutral`, `empty_is_an_emotion_again`, `pain_is_not_an_emotion` | 🟡 (text-sentiment; audio SER unwired in prod) |
| Local time stated correctly, no UTC offset (D-17/21) | spec §12 | `engine_gate` `localtime_spain` (zoneinfo check); `test_e3_now_section.py` | `the_model_does_the_timezone_arithmetic` | ✅ |

## D. Memory correctness (spec §4–§8)

| Requirement | Source | Instrument | Killing mutation | Status |
|---|---|---|---|---|
| Episodic retrieval (dense+BM25→RRF) | spec §5 | `tests/golden/test_gs1_memory.py` | — | 🟡 (retrieval works; RAGAS quality 🔴) |
| **Entity resolution accuracy** | spec §8 | `scripts/measure_entity_resolution.py` (acc 1.000), `gs2` | `entity_resolution_ignores_the_score_order`, `resolve_the_whole_utterance` | ✅ |
| Semantic/temporal facts (Graphiti/Neo4j) | spec §6 | §6 integration (real Neo4j) | — | 🟡 |
| Procedural rules (confidence gates) | spec §7 | §7 unit/integration | — | 🟡 |
| **Never re-store on recall (no double-write)** | design §4, CLAUDE §2 | — | — | 🔴 |
| Write routing (fact→semantic, event→episodic, style→persona) | spec §5/§6 | persona spot test | — | 🔴 (as a matrix) |

## E. Search / live-info decision (spec §15) + verified retrieval

| Requirement | Source | Instrument | Killing mutation | Status |
|---|---|---|---|---|
| Volatility / needs-live-info decision | spec §15 | `tests/labeled/volatility.jsonl` (174 labeled), `scripts/measure_classifiers.py` (eff. recall 0.989) | `vol_always_false`, `live_lookup_always_false` | ✅ |
| Emotional turn takes no web tool (D-14) | design §6/§16 | `engine_gate` `overclaim_bait` | `an_emotional_turn_still_searches`, `external_tools_are_always_offered` | ✅ |
| Honest search-failure line (no stale training answer) | design §16 | `test_e5_caller_independence.py` | `the_fallback_answers_a_volatile_turn_from_training_data` | ✅ |
| Caller independence (text≡voice decision) | — | `scripts/caller_independence_probe.py` | `simple_turns_skip_the_classifier` | ✅ |
| **Read the page + cross-check + recency (verified retrieval)** | design §15 intent | `tests/retrieval/` harness (VerifiedResult cardinalities, recency, mutation) | (its own, being built) | 🔵 |

## F. Tools & projects (spec §13, §14, §16)

| Requirement | Source | Instrument | Status |
|---|---|---|---|
| Tool dispatch + ReAct loop (validated JSON steps) | spec §13 | acceptance (project-flow) | 🟡 |
| Background delivery = social act (waiter model, de-dup) | spec §14 | §14 delivery tests | 🟡 |
| Projects: avg-cost P&L, consent-gated insight | spec §16 | §16 tests | 🟡 |

## G. Learning & psychology (spec §17, §18)

| Requirement | Source | Instrument | Status |
|---|---|---|---|
| OCEAN nudges, mood baseline/deviation, stage-of-change | spec §17 | §17 unit tests (clinical-language render) | 🟡 (inference quality human-validated, not automated) |
| Session-close consolidation (rule reinforce/contradict/add) | spec §18 | `tests/acceptance/test_consolidation_flow.py` | 🟡 (order-dependent, D-11) |

## H. Voice I/O (spec §19–§24) — ⬜ out of current scope

VAD, semantic endpointing, barge-in, STT, TTS, SER all have unit/integration tests
(`tests/unit/test_pipecat_processor.py`, §19–24), but real-mic barge-in is hardware-blocked and
SER is unwired in prod (`ser_service_url` empty). **Not re-verified in this pass** per the
current core-engine scope.

## Cross-cutting meta-evaluation

| Property | Instrument | Status |
|---|---|---|
| **The checks themselves can fail** | `scripts/mutation_audit.py` (37/38 killed; 1 control survives) | ✅ |
| Judge ↔ human agreement (is the judge trustworthy?) | judge calibrated vs human labels (small set) | 🟡 (agreement not reported per-run) |
| Latency (first-audio / turn) | `engine_gate` timings, `scripts/latency_benchmark.py` (median+p95) | 🟡 (measured, **not gated**) |
| **Multi-turn behaviour** (context carry, correction, drift) | — (every gate scenario is single-turn) | 🔴 |
| **Intent classification accuracy** (indirect phrasings) | — (no labeled set) | 🔴 |
| Golden sets run verbatim vs their own `must_not` lists | `GOLDEN_SETS*.json` exist but inform scenarios, not executed case-by-case | 🟡 |

---

## The gaps, ranked (what to close first)

1. 🔴 **Multi-turn evaluation** — the biggest structural blind spot for a *companion*; today every
   scenario is one turn. Add a scripted-conversation harness with per-turn trace assertions
   (context carry, correction, drift) + a mutation on context carry-over.
2. 🔴 **Memory-write correctness** — assert on the *write* trace that a recall turn writes nothing
   new ("never re-store on recall"), and that routing lands facts/events/style in the right store.
3. 🔴 **Intent classification** — the one classifier with no labeled set; build one (utterance →
   expected action) and measure precision/recall like volatility already is.
4. 🟡 **Judge calibration reported per run** — the judge underpins every subjective ✅; publish its
   human-agreement each run so it can't silently drift.
5. 🟡 **RAGAS on retrieval** + 🟡 **trait A/B** (prove traits aren't decorative) + 🔴 **formality metric**.
6. 🟡 **Gate what's measured** — put thresholds on latency p95 and `chatbot_like`; run the full gate
   nightly and track the trend, not one number.

**Rule for closing any of these:** it lands only with its killing mutation. A new ✅ that has no
red-turning mutation is not a ✅ — it is a 🟡 wearing a green tick.

_Last updated: 2026-07-11. Verified-retrieval rows update when that branch merges; voice rows
update when voice re-enters scope._
