# Defects found — core engine test session

**None of these were fixed.** The session brief forbids product fixes; the job was to find
out what is true. Each defect below has a file, a line, a reproducing input, and — where
possible — a deterministic test that goes red at HEAD.

Run the reproducers:

```
uv run pytest -m defect                     # deterministic; 5 red at HEAD
uv run pytest -m "not real_call and not defect"   # the green suite
uv run python -m scripts.caller_independence_probe --repeats 3
uv run python -m scripts.measure_classifiers --real --repeats 5
```

Severity: **S1** the user gets a wrong or absent answer · **S2** the companion breaks its own
design contract · **S3** waste or latent hazard.

| id | severity | one line | reproducer |
|---|---|---|---|
| D-1 | S1 | deterministic volatility gate recall 0.839, below the 0.95 bar | `test_e2_volatility_classifier.py` |
| D-2 | S1 | `generate()` never computes `needs_live_info`; honest search-failure lines unreachable | `test_e5_caller_independence.py` |
| D-3 | S2 | register is caller-dependent: same input, different delivery | probe |
| D-4 | S1 | whether a search runs flips between identical runs | probe |
| D-5 | S1 | `"empty"` is parsed as the emotion *sad* — neutral turns delivered `down` | `test_e3_prosody_read.py` |
| D-6 | S2 | every gate (reflection, overclaim, disclosure, curiosity) is skipped on fallback turns | `test_e1_enforcement.py` |
| D-7 | S2 | a draft the engine flagged as assistant-speak ships as the final reply | `test_e1_enforcement.py` |
| D-8 | S1 | the search acknowledgement survives as the final answer | `test_e1_enforcement.py` |
| D-9 | S1 | a non-`LLMUnavailable` dependency failure escapes the turn → the user hears silence | `test_e1_enforcement.py` |
| D-10 | S3 | `OpenRouterLLM.stream()` guards stream creation but not stream consumption | — |
| D-11 | S3 | `test_consolidation_flow` passes alone, fails in the full suite (order-dependent) | — |

---

## D-1 — the deterministic volatility gate misses 14 of 87 volatile questions

**Severity S1.** `core/reasoning/volatility.py:85` · `core/reasoning/response_gen.py:1512`

Measured over `tests/labeled/volatility.jsonl` (174 questions, 87 volatile / 87 stable):

| classifier | precision | recall | F1 | over-triggers on stable |
|---|--:|--:|--:|--:|
| `is_volatile_question` | 0.981 | **0.598** | 0.743 | 0.011 |
| `_is_live_info_query` | 1.000 | **0.391** | 0.562 | 0.000 |
| both OR'd — the gate | 0.986 | **0.839** | 0.907 | 0.011 |
| `needs_live_info` (real LLM) | 0.976 | 0.954 | 0.965 | 0.023 |
| **effective gate** (LLM ∨ deterministic) | 0.966 | **0.989** | 0.977 | 0.034 |

Gate: recall ≥ 0.95 on the volatile class. The deterministic backstop **fails at 0.839**.

This matters because the LLM classifier is not always consulted (see D-2) and is itself
unstable (D-4). Over 5 runs its recall was 0.943 / 0.954 / 0.954 / 0.966 / 0.966 — **median
0.954, and below the 0.95 bar on 1 run in 5.**

The complete false-negative list for the deterministic gate — each of these is a question
whose true answer changes over time, answered from training data:

```
who's leading the polls?                who manages Manchester United now?
LTP of OP                               what's gold going for?
give me a quote on NABIL                what did the NEPSE index close at?
how cold is it in London?               is it snowing in the mountains?
do I need a jacket?                     where is Arsenal in the table?
did Nepal win?                          what's the newest iPhone?
how many users does it have now?        my portfolio is stressing me out, how's OP doing
```

`"LTP of OP"` is the original SRC1 defect from `SESSION_REPORT_F1-F6` §F5. It is **still
present**, and it is the one question the *effective* gate also misses (the LLM called it
volatile on only 3 of 5 runs).

False positive worth noting: `"is it worth learning Rust?"` triggers a web search, because
`_MOVING_VALUE` matches the word `worth`.

**Not measured, and it should be:** the brief also asks for labeled precision/recall on
entity resolution, the `style_flags` detector, and intent classification. Only the detector
has partial coverage (`tests/golden/test_style_judge_agreement.py`, against 
frozen judge labels). Entity resolution and intent classification remain unmeasured — see
"What this session did not do" at the bottom.

---

## D-2 — `generate()` never forms a volatility opinion, so it cannot report an honest failure

**Severity S1.** `adapters/orchestrator/langgraph_orchestrator.py:217`

```python
if prompt.complexity_hint == "simple":
    self._span("reasoning", node="resolve_context", skipped="simple_turn_fast_path")
    return {"resolution": _Resolution()}       # needs_live_info stays None
```

`generate_spoken()` calls `_resolve_note()` unconditionally and never takes this shortcut.

**Measured:** across 21 text turns in `docs/quality/caller_independence.json`,
`needs_live_info` was `None` **21/21** times and `context_skipped == "simple_turn_fast_path"`
**21/21** times. 170 of the 174 labeled questions are `simple` under the word-count
heuristic in `prompt_assembly.py:749`, including *"who is the current prime minister of
Nepal?"*.

The turn still usually searches — the agentic loop lets the model request `web_search`
itself — so this is **not** "the text path never searches". (An earlier draft of this report
claimed that; the probe disproved it.) The real damage is downstream. Three behaviours are
gated on `prompt.needs_live_info is True`, and are therefore **unreachable** from
`api/routes/chat.py`:

- `_SEARCH_FAILED_TEXT` (`response_gen.py:534`) — *"I tried to look that up just now and
  couldn't get through."* A failed search on the text path silently ships the model's
  training-data answer instead. This inverts design §16's honesty rule.
- `_NOT_FOUND_TEXT` (`response_gen.py:542`) — same, for a search that returned nothing.
- `suppress_live_search`, and the `emotional_read` that feeds the register (D-3).

---

## D-3 — the delivery register depends on which edge called the engine

**Severity S2.** `adapters/orchestrator/langgraph_orchestrator.py:427`

`_augment()` derives `prompt.emotion` from the reasoning step's `emotional_read`. The text
path skipped that step (D-2), so `emotional_read` is `""`, so `emotion` is `None`, so
`read_register()` returns `"neutral"` — always.

Measured over 7 utterances × 3 runs: `register` diverged by caller on **5 of 7** utterances.
`"I'm feeling really low today"` is delivered `neutral` through `generate()` and `down`
through `generate_spoken()`, 3 runs out of 3.

Design §3.6.5 makes the emotional read decide the tone. Half the callers don't have one.

---

## D-4 — whether a search runs is not stable across identical runs

**Severity S1.** Nondeterminism in `context_intent` + the model's own `tool_request`.

`"did Nepal win?"` — `searched` differed by caller on 3/3 runs, in **both directions**
(`False → True` on some runs, `True → False` on others). `"what's gold going for?"` diverged
on 2/3.

Per-question instability of the real classifier over 5 runs (`docs/quality/volatility_drift.txt`):

```
[False, True,  False, False, False]  who is the CEO of Apple?
[True,  True,  True,  False, False]  LTP of OP
[False, False, True,  False, True ]  look into that company for me
[True,  False, True,  True,  True ]  how many OP do I have?      (should be False — internal)
```

A user asking the same question twice gets a live answer once and a stale one the next time.
Single-sample verification of any staleness fix is meaningless.

---

## D-5 — the word `"empty"` is parsed as the emotion *sad*

**Severity S1** (it mis-delivers every neutral spoken turn). `core/reasoning/prosody.py:118`

The context prompt instructs the model:

```
"emotional_read": "<the feeling, or empty>"
```

Models comply literally and emit `"emotional_read": "empty"` on neutral turns — observed
directly in the probe logs. `emotion_from_text()` treats `""`, `"neutral"`, `"none"`,
`"calm"`, `"n/a"` and `"-"` as "no signal", but **not** `"empty"`. It falls through to the
regex families, and the *sad* pattern lists `empty` (intended for *"I feel empty"*):

```python
r"\bsad\b|sadness|down|low\b|grief|…|numb|empty|mourning|loss"
```

```
>>> emotion_from_text("empty")
{'label': 'sad', 'valence': -0.5, 'arousal': 0.2, 'confidence': 0.6, 'source': 'text'}
>>> read_register(_)
'down'
```

**Measured on the spoken path:** `"what's 15% of 240?"` → register `down`, **3 runs of 3**.
`"who is the current prime minister of Nepal?"` → `down`, 3/3. `"hi"` → `down`, 2/3.

So the one deployment where dynamic prosody finally *does* fire (C3 wired the text-sentiment
fallback) fires it **backwards**: the companion answers arithmetic in a sad voice.

A second, quieter half of the same bug: the design doc's own worked example —
*"what's happening in Nepal … gives me a lot of pain"* → `emotional_read: "pain"` — produces
**no emotion at all**, because `pain` appears in no pattern (`hurt` does; `pain` does not).

```
>>> emotion_from_text("pain") is None
True
```

The flagship emotional scenario gets a neutral register; a greeting gets a sad one.

---

## D-6 — every behaviour gate is skipped on exactly the turns that need them most

**Severity S2.** `core/reasoning/response_gen.py:468, 475, 481, 484`

`ResponseGenerator.generate()` returns through `_finish()` directly — never
`_finalize()` → `_apply_gates()` — on four paths:

| line | condition | what it ships |
|---|---|---|
| 468 | cost ceiling tripped, no draft | `_SAFE_FALLBACK_TEXT` |
| 475 | judgment JSON invalid twice | `last_draft` |
| 481 | plain-reply fallback | `plain` |
| 484 | provider fully down | `_SAFE_FALLBACK_TEXT` |

`_apply_gates()` is where the curiosity gate, `check_boundary()`, `_warm_disclosure()` and
self-reflection live. So a fallback reply — the least trustworthy output the engine
produces — is the only one nothing critiques.

This directly violates CLAUDE.md §2 ("self-reflection is a first-class step, not a bolt-on")
and design §5.2 (the overclaim rule layer runs "*before* it reaches TTS").

Measured: the `reflection` span was absent on **4 of 21** text turns and **2 of 21** spoken
turns in the probe. Judgment-JSON validation failures are common — the probe log shows them
on most turns, frequently on both attempts.

Reproduced deterministically:

```
tests/engine/test_e1_enforcement.py::test_self_reflection_runs_even_when_the_judgment_json_is_invalid
tests/engine/test_e1_enforcement.py::test_the_overclaim_guard_runs_even_on_a_fallback_reply
```

The second is the sharper one: with two malformed JSON responses, the draft
*"I understand exactly how you feel, I feel your pain too."* — the exact phrase design §1.4
forbids — reaches the user unrewritten.

---

## D-7 — the detector detects; nothing enforces

**Severity S2.** `core/reasoning/response_gen.py:1336`

```python
style_flags = find_forbidden(clean_text, allow_disclosure=allow_disc)
if style_flags:
    logger.warning("response contains forbidden assistant-speak: %s", style_flags)
return GenerationResult(final_text=clean_text, …, style_flags=style_flags)
```

It logs, and returns the reply anyway. A `GenerationResult` with non-empty `style_flags` is
by construction a reply the engine itself judged to be assistant-speak.

Observed live, twice in 42 probe turns, both through `generate()`:

```
['nature monologue']          "Look, I do pay attention to what you tell me — I remember the stuff…"
['volunteered AI disclaimer'] "Look, I do pay real attention to you — what you've told me, what matte…"
```

Reproduced deterministically when the repair loop exhausts:
`test_a_draft_carrying_style_flags_never_becomes_the_final_reply` → ships
`"How can I help you today?"` with `style_flags=['service-desk opener']`.

This is `SESSION_REPORT_GATE_RERUN` §3.2(a), still open, and now shown to affect the **text**
path too — not just the spoken one.

---

## D-8 — the search acknowledgement survives as the final reply

**Severity S1.** `core/reasoning/response_gen.py:475`

```python
if turn is None:                       # both JSON attempts failed
    if last_draft.strip():
        return await self._finish(prompt, _sanitize_tags(last_draft), "respond", None)
```

On a live-info turn, `last_draft` is the model's **holding line** — the thing it said while
kicking off a search. A JSON glitch on a later step therefore ships the ack as the answer.

```
"Oh, you're looking for the current Last Traded Price for OP again.
 I'll check that for you right now."     ← this was the final spoken reply
```

`_needs_capability_repair()` already recognises that exact shape (`_HOLLOW_PROMISE`). Nothing
consults it before returning `last_draft`. Same root cause as D-6: the bypass at line 475.

This is `SESSION_REPORT_GATE_RERUN` §3.2(b). Reproduced by
`test_the_search_acknowledgement_never_becomes_the_final_reply`.

---

## D-9 — a dependency failure that isn't `LLMUnavailable` escapes the turn

**Severity S1.** `core/reasoning/response_gen.py:598, 654, 1087` (and `1220`)

The engine degrades gracefully **only** for `LLMUnavailable`. Verified by driving a
provider that fails every call:

| exception | `generate()` | `generate_spoken()` |
|---|---|---|
| `LLMUnavailable("read timeout")` | safe fallback reply ✅ | safe fallback reply ✅ |
| `RuntimeError("ReadTimeout")` | **raises** ❌ | **raises** ❌ |
| mid-stream `APIError` | safe fallback ✅ | safe fallback ✅ |

`_build_search_query`, `_warm_disclosure` and `_rewrite_assistant_speak` each catch
`LLMUnavailable` and nothing wider. An `httpx.ReadTimeout` or `openai.APIError` raised from
any of them escapes `generate()`, is swallowed by the broad `except Exception` in
`VoiceSession._run_turn_inner`, and the turn produces `reply=""` — the silence in
`SESSION_REPORT_GATE_RERUN` §3.1.

**The fix must not be a blanket `except Exception`.** `core/errors.py` (F3) exists precisely
because that hid a `TypeError` for months. `test_a_programming_error_still_fails_loudly`
passes today and guards the fix.

---

## D-10 — `stream()` guards stream creation, not stream consumption

**Severity S3 (latent).** `adapters/llm/openrouter.py:289-297`

```python
try:
    stream = await self._client.chat.completions.create(**kwargs)
except Exception as exc:
    raise LLMUnavailable(...) from exc

parts: list[str] = []
async for chunk in stream:        # ← outside the guard
```

An error raised while consuming the SSE body propagates raw, violating the `LLM` port's
contract that provider failures surface as `LLMUnavailable`. Observed live during the probe:

```
openai.APIError: JSON error injected into SSE stream
```

Today it is absorbed by `generate_spoken`'s `except Exception` and the turn recovers, so
nothing user-visible breaks. It is filed because it is the same class of hazard as D-9 and
will bite the first caller that doesn't have a broad catch.

---

## D-11 — `test_consolidation_flow` is order-dependent

**Severity S3 (test defect, not product).**

`tests/acceptance/test_consolidation_flow.py::test_session_close_learns_via_queued_consolidation`
passes in isolation and failed once inside the full suite with `-p no:randomly`, then passed
on a re-run. Shared state between tests (Redis queue or Mongo) is the likely cause. Left
unfixed: the brief scopes this session to engine tests, and this is an acceptance test for
the consolidation worker.

---

## Corrections to earlier reports

Two claims in the inherited documentation do not reproduce, and are worth writing down so
nobody re-derives them:

1. **`context_intent` returns unusable JSON "roughly 1 call in 6"**
   (`core/reasoning/volatility.py:8`). Over 174 real calls: **0 unusable verdicts after the
   built-in retry.** Raw first attempts do fail regularly (visible in the probe log), but
   `_resolve_note`'s retry-on-a-stronger-tier absorbs every one of them. The docstring
   describes per-attempt failure and reads as if it were per-call.

2. **"the voice path is the deprived one."** The premise of the session brief — that
   `_finalize`'s gates "never run on the spoken path" — was fixed by C1 and is no longer
   true: `_apply_gates` is called by both `_stream_reply` and `_finalize`. The surviving
   asymmetry runs the *other* way (D-2, D-3): it is `generate()`, the **text** path, that
   skips a step. The `complexity_hint` simple-turn gate does still live only in
   `_resolve_context`, exactly as the brief said — but its effect is the opposite of the one
   predicted.

---

## What this session did not do

Stated so the gaps are not mistaken for passes:

- **Entity resolution accuracy** (ambiguous tickers, with/without the portfolio seeded) is
  **not measured**. No labeled set was built. `docs/ENGINE_QUALITY_GATE.md` records it as
  UNMEASURED, not as passing.
- **Intent classification accuracy on indirect phrasings** is **not measured**.
- **Detector–judge agreement** is asserted but not quantified: `test_style_judge_agreement.py`
  checks the detector against frozen judge labels from `docs/quality/baseline_live.json` and
  the curated gs3 examples, and passes — but no precision/recall number is computed.
- The E3 integration bundles and the full E4 drift matrix (N ≥ 5 per scenario, per-step
  latency/tokens/cost from the trace) are **partial**: the caller-independence probe covers
  7 utterances × 3 runs; the golden-set scenarios in `docs/GOLDEN_SETS*.json` were not run
  end to end with trace assertions.
