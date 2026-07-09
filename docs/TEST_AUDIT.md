# Test audit — the core reasoning engine

**Scope:** the engine only. Voice input (VAD, endpointing, STT) and voice output
(register→prosody→TTS, tag stripping, barge-in) are transducers around the engine and were
not audited.

**The question every test had to answer: can you fail?** Not "do you pass" — 594 passing
tests coexisted with an app that produced total silence on every live turn. A test that
cannot go red while the feature it names is broken is not evidence. It is decoration.

---

## 1. Method

`scripts/mutation_audit.py`. For each mutation: break exactly one engine behaviour, run the
engine test subset, record which tests go red, restore the file.

```
uv run python -m scripts.mutation_audit          # the full matrix
uv run python -m scripts.mutation_audit --list   # what each mutation breaks
```

18 mutations, including one deliberate **no-op control** (`judgment_validation_skipped`,
which adds `extra="allow"` to a Pydantic model). The control kills nothing — as it must. If
it had killed anything, the harness would be measuring test-ordering, not behaviour, and
every other row would be worthless.

The mutations are textual anchors against real source. They are listed in the script with a
one-line statement of the engine claim each falsifies.

---

## 2. Counts

| | before | after |
|---|--:|--:|
| test files | 114 | 118 |
| tests collected | 653 | 713 |
| green by default (`-m "not real_call and not defect"`) | 653 | 654 |
| RED on purpose (`-m defect`) | 0 | 9 |
| real-call | 43 | 50 |
| **deleted** | | **12** |
| **added** | | **72** |

The `defect` tests are new. They assert what the design requires, fail at HEAD, and turn
green when the defect is fixed. Each names its entry in `docs/DEFECTS_FOUND.md`.

---

## 3. The finding

**Six of eighteen mutations survived the entire 653-test suite.** Nothing anywhere noticed
when these behaviours were deleted from the engine:

| mutation | what it deletes |
|---|---|
| `live_lookup_always_false` | a turn the reasoning step judged volatile never routes to a search |
| `capability_repair_disabled` | the forced-search backstop never runs; a refusal ships as the answer |
| `search_query_is_raw_utterance` | the resolved entity never reaches the query; `OP` becomes the crypto token |
| `warm_disclosure_disabled` | a vulnerable nature question gets the cold, unpolished draft |
| `degenerate_rewrite_accepted` | a one-word rewrite passes as "cleaner" than the real reply |
| `query_echo_not_stripped` | the raw search query is spoken aloud to the user |

**Four of those six are the fixes the previous two sessions shipped** (S1, S2, S4). They were
committed, written up, and never covered. A grep confirms it independently — these symbols
were referenced by **no test in the repository**:

```
_requires_live_lookup    _build_search_query    _strip_query_echo
_warm_disclosure         _is_degenerate_rewrite
```

`_capability_repair` appeared to have coverage in `tests/unit/test_audit_fixes.py`, but that
file tests `_needs_capability_repair` — the *detector* that decides whether a repair is
needed — not `_capability_repair`, the *action* that performs it. The names differ by four
characters and the coverage differs by everything.

All six are now killed by `tests/engine/test_e1_steps.py`. The audit was re-run to prove it,
not assumed.

### The structural cause

`tests/acceptance/test_core_engine_e2e.py` — the file whose name promises exactly this
coverage — drove `pipeline.generator`, the bare `ResponseGenerator`. Production
(`api/routes/chat.py:143`) drives `pipeline.orchestrator`, the `LangGraphOrchestrator`. The
orchestrator's `resolve_context` node is what produces `needs_live_info`. So the "core engine
e2e" suite was exercising an engine the application does not run, and `live_lookup_always_false`
walked straight through it. Repointed at `p.orchestrator`.

---

## 4. Deletions

### `tests/golden/test_gs3_judge.py` — deleted (7 tests)

- **What it drove:** canned reply strings from `gs3_judge.json`. Never engine output.
- **Can it fail?** It never *ran*. `pytestmark` skipped the module unless `RUN_GS3_JUDGE=1`,
  which nothing sets. Zero of its 7 tests had executed in CI.
- **Verdict: delete.** Its one legitimate job — calibrating the LLM judge against human
  labels — is already done by `tests/real_call/test_judge.py`, which uses the same canonical
  cases (`"hi" → "How can I help you?"` must fail; a warm reply must pass) and actually runs
  under `-m real_call`. `gs3_judge.json` is kept: `tests/golden/test_style_judge_agreement.py`
  uses its labels as frozen ground truth for the detector.

### `test_real_model_never_talks_like_a_service_desk` — deleted (5 parametrisations)

- **What it drove:** a bare `ResponseGenerator` with a hand-composed system prompt. Not the
  wired engine.
- **Can it fail?** No. `@pytest.mark.xfail(reason=…, strict=False)`. Under `strict=False`
  neither an unexpected pass nor a failure changes the build result. These five tests were
  the entire "5 xpassed" column in every suite run for months, including the run in
  `SESSION_REPORT_GATE_RERUN` §5 — while the judge was simultaneously scoring 2 of 11 live
  scenarios `chatbot_like`. The test was green-adjacent and the app was wrong.
- **Verdict: delete.** Real-model tone is now measured against a threshold in
  `scripts/engine_gate.py`, where a miss is a reported gate failure rather than an invisible
  xpass.

Its helpers (`_prompt`, `_TEMPTING_OPENERS`, `_faithful_system_prompt`) died with it.

---

## 5. Tests fixed because they were wrong *as tests*

The brief permits this and forbids product fixes. Both of these were masking, not measuring.

### `tests/golden/test_gs5_isolation.py`

```python
    except Exception as exc:
        pytest.skip(f"semantic isolation probe skipped (Graphiti unavailable): {exc}")
    finally:
        ...
    assert leaks == [], "MULTI-TENANT ISOLATION BREACH (critical):\n" + …
```

The `assert` sat **after** a `pytest.skip` buried in the Graphiti probe's `except`. Leaks
from episodic, entity resolution, the self-model and procedural memory were all collected
into `leaks` *above* that point — and then discarded, unasserted, whenever Graphiti was
unreachable. A real breach of the §0.5 hard invariant would have reported as **"skipped"**.

Split into two tests. The graph probe may now be unavailable without silencing the other four.

### `tests/acceptance/test_core_engine_e2e.py`

Repointed from `p.generator` to `p.orchestrator` (see §3).

---

## 6. Per-file verdicts

`mutation-proven` = at least one mutation in this session's set turned it red.
`not covered by this set` is **not** a pass — it means the mutation set targeted the
reasoning core, and this file's subject (a store, a worker, a queue) had no mutation aimed at
it. Those files were reviewed by reading, not by proof, and are marked accordingly.

### Mutation-proven — keep

| file | tests | killed by |
|---|--:|---|
| `tests/golden/test_gs3_style.py` | 33 | `detector_never_flags` (23), `scrub_forbidden_is_identity` (3) |
| `tests/golden/test_style_judge_agreement.py` | 12 | `detector_never_flags` (8) |
| `tests/unit/test_response_gen.py` | 21 | `reflection_never_runs`, `style_flags_never_reported`, `curiosity_gate_always_responds`, `boundary_never_flags`, `cost_ceiling_never_trips`, `scrub_forbidden_is_identity` |
| `tests/unit/test_volatility.py` | 22 | `vol_always_false` (10) |
| `tests/unit/test_self_model.py` | 7 | `boundary_never_flags` (4) |
| `tests/unit/test_prosody.py` | 11 | `register_always_neutral` (5) |
| `tests/unit/test_tool_leak.py` | 5 | `tool_leak_not_stripped` (3) |
| `tests/golden/test_gs3_behavioral.py` | 15 | `boundary_never_flags` (3), `curiosity_gate_always_responds`, `tag_sanitizer_is_identity` |
| `tests/unit/test_audit_fixes.py` | 8 | `tag_sanitizer_is_identity` |
| `tests/engine/test_e1_steps.py` | 22 | all six previously-surviving mutations |
| `tests/engine/test_e2_volatility_classifier.py` | 13 | `vol_always_false` |

### Improved

| file | change |
|---|---|
| `tests/acceptance/test_core_engine_e2e.py` | drove the wrong engine; repointed at `p.orchestrator` |
| `tests/golden/test_gs5_isolation.py` | a `pytest.skip` was discarding hard-invariant findings |
| `tests/support/real_pipeline.py` | gained `say_spoken()` and durable-span read-back, so a test can finally ask *"did self-reflection run?"* instead of inferring it from the reply |

### Deleted

| file / test | tests | reason |
|---|--:|---|
| `tests/golden/test_gs3_judge.py` | 7 | canned strings; skip-gated so it never ran |
| `test_real_model_never_talks_like_a_service_desk` | 5 | `xfail(strict=False)` — cannot fail or pass |

### Not covered by this mutation set

These were read, not proven. They test stores, workers, queues, adapters, and the voice
transducers — the mutation set aimed at the reasoning core. Naming them here is the honest
alternative to implying the audit reached them.

`test_prompt_assembly` · `test_dispatcher` · `test_entities` · `test_episodic` ·
`test_semantic` · `test_procedural` · `test_memory_routing` · `test_recall_routing` ·
`test_extraction` · `test_persona` · `test_psych_model` · `test_tool_results` · `test_steps` ·
`test_localtime` · `test_prompt_cache` · `test_llm_router` · `test_model_selection` ·
`test_web_search` · `test_turn_evaluator` · `test_turn_error_handling` · `test_projects` ·
`test_trace_totals` · `test_working_memory` · `test_gs1_memory` · `test_gs2_entities` ·
`test_gs4_learning` · `test_gs5_isolation`

Two of these deserve a follow-up mutation each, and did not get one: `test_gs5_isolation`
(delete the `user_id` filter from the Qdrant query and confirm it goes red) and
`test_gs2_entities` (return the wrong candidate and confirm it goes red). Multi-tenant
isolation is a hard invariant; it should not remain unproven.

---

## 7. What replaced the deletions

| file | tests | what it holds |
|---|--:|---|
| `tests/engine/test_e1_steps.py` | 22 | the six untested steps; every mutation now dies |
| `tests/engine/test_e1_enforcement.py` | 8 | enforcement + gate reachability; 5 are `defect` |
| `tests/engine/test_e2_volatility_classifier.py` | 13 | the labeled set as a regression guard |
| `tests/engine/test_e3_prosody_read.py` | 25 | the emotional-read → register seam; 3 are `defect` |
| `tests/engine/test_e5_caller_independence.py` | 8 | the two callers must agree; 6 are `real_call` |
| `tests/labeled/volatility.jsonl` | — | 174 labeled questions, 87 volatile / 87 stable |

---

## 8. The `defect` marker, and why it is not `xfail`

An `xfail(strict=False)` test is invisible in both directions. It was the mechanism by which
a known tone failure sat unexamined for months. The replacement:

```
uv run pytest -m defect                            # 9 red, deliberately
uv run pytest -m "not real_call and not defect"    # 654 green
```

A `defect` test asserts what `docs/ai-companion-design-doc.md` requires, cites its entry in
`docs/DEFECTS_FOUND.md`, and fails today. When the defect is fixed the test goes green and the
marker is deleted. Nothing is hidden, and the count of red tests is the count of known defects.

Each defect file carries a **control** — a test on the same code path that passes — so a
reproducer can never be red for an accidental reason. `test_a_valid_turn_runs_self_reflection`
and `test_a_wrapped_provider_outage_degrades_to_an_honest_reply` exist for exactly that.

---

## 9. Honest limits of this audit

- The mutation set is 18 mutations over the reasoning core. It is not exhaustive, and a file
  it never touched is unproven, not proven-good (§6).
- `_capability_repair`, `_build_search_query` and `_warm_disclosure` are now covered by
  tests that use a `FakeLLM`. The *step* is real; the model's reply is scripted. That is
  correct for asserting **what the engine sends and how it treats what comes back**, and it is
  not a substitute for the real-call gate in `scripts/engine_gate.py`.
- Entity resolution and intent classification have **no labeled measurement** (see
  `docs/DEFECTS_FOUND.md`, "What this session did not do"). The volatility classifier is the
  only one measured properly.
- `tests/acceptance/test_consolidation_flow.py` is order-dependent: it passes alone and failed
  once inside the full suite. Recorded as D-11, unfixed.
