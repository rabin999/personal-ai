# Test Report — Real End-to-End Scenarios, Judged

Append-only. Every scenario records: the app-goal it verifies, the REAL captured conversation
(verbatim in/out), the trace summary (steps + per-step latency/tokens/cost), the LLM-judge's
per-rubric analysis + verdict + defects, the fix applied, and the re-run result.

Nothing fabricated. If blocked, states exactly what a human must verify.

**Run started:** 2026-07-07. Environment: real Mongo/Qdrant/Neo4j/Redis (docker) + real
OpenRouter/Serper/X-AI keys.

---

## Item 1 — Barge-in / interruption (spec §24)

**App-goal verified:** while the companion is speaking, if the user talks it STOPS immediately,
cancels the in-flight generation + queued TTS, and responds to the new input with context intact.

**Verification method:** barge-in is a runtime *control-flow* property, independent of which model
wrote the reply, so it is driven deterministically through the **real `VoiceSession._consume`
state machine** (real `WorkingMemory`, real `SemanticEndpointer`, real VAD gate + audio pipeline)
with controllable STT/TTS/generator collaborators. Using the live LLM here would add
non-deterministic timing without changing the property under test (a justified §0.1 call, logged
in REMEDIATION_LOG). File: `tests/e2e/test_barge_in_engine.py`.

### Scenario 1 — interrupt mid-reply, then switch topic
Timeline (synthetic 20ms frames): utterance "tell me about the ocean." → endpoint → turn 1 starts
streaming TTS → **sustained fresh speech (12 frames) over the playing reply** → interrupt →
new utterance "wait, what's the weather in tokyo?" → turn 2.

Captured assertions (all passed):
- Trace contains a `barge_in` stage event.
- In-flight generation received `CancelledError` (`gen.interrupted >= 1`).
- TTS stream was closed on barge-in (`tts.cancelled`).
- Two turns ran (`turns_started >= 2`).
- Interrupted reply did not play in full (30 chunks vs 50 for two uninterrupted replies).
- Working memory retained BOTH the prior turn ("ocean") and the new utterance ("tokyo") —
  context intact across the interrupt.

### Scenario 2 — short echo blip must NOT falsely interrupt (the reported self-interrupt bug)
A 4-frame speech blip (below the 8-frame `_BARGE_IN_FRAMES` guard — models a residual-echo
transient even with AEC on) during playback. Assertions (passed): NO `barge_in` event, reply not
cancelled (`interrupted == 0`), reply completed and all 30 chunks played. This proves the guard
that keeps the companion from stopping for its own audio.

### Scenario 3 — interrupt then continue the SAME topic keeps context
"tell me about black holes." → interrupt → "wait — but how big can they get?". Assertions
(passed): `barge_in` fired, two turns ran, working memory still holds "black holes" thread + the
follow-up. Proves conversational continuity across a barge-in.

**Result:** 3/3 scenarios pass against the real state machine. `uv run pytest
tests/e2e/test_barge_in_engine.py` → `3 passed`.

### Defects found & fixed this item
1. **Pipecat engine path never enabled interruptions.** `voice/pipecat/runtime.py` ran
   `PipelineParams()` (Pipecat default `allow_interruptions=False`) while the docstring *claimed*
   barge-in was framework-driven — so on the Pipecat engine the bot talked over the user. Fixed:
   `PipelineParams(allow_interruptions=True)`.
2. **Pipecat CompanionProcessor awaited full generation inline**, so an interruption mid-generation
   could not cancel it and a stale `TextFrame` could be pushed after the interrupt. Fixed: the reply
   now runs as a cancellable task; a `StartInterruptionFrame` (and any superseding final transcript)
   cancels it — mirroring the native runtime's `turn.cancel()`.

**Blocked (needs hardware/human):**
- The **browser mic + AEC** end of full-duplex barge-in cannot be exercised without a real
  microphone. Manual step: open the web app, start a conversation, and while the companion is
  speaking, talk over it — verify playback stops within ~250ms and the companion answers the new
  utterance. (Server + client wiring audited: full-duplex mic streaming during playback in
  `web/src/pages/CompanionPage.tsx`, `echoCancellation:true` in `web/src/lib/audio.ts`, client
  flushes playback on the `barge_in` trace event.)
- The **Pipecat engine path** cannot be run here: the optional `voice` extra (pipecat/silero) is
  not installed in this environment, so `voice/pipecat/*` does not import. The two fixes above are
  code-correct per Pipecat's documented API; runtime verification needs `uv sync --extra voice`
  + a mic.

---

## Item 2 — Response quality: companion, not chatbot (continuous top goal)

**App-goal verified:** across many conversation types, the companion sounds like a warm, present
friend — never a generic AI assistant (rubric #1). Verification = REAL turns (real OpenRouter LLM
+ real Mongo/Qdrant/Neo4j/Redis, `u_demo_001`) driven through `ResponseGenerator.generate`, each
reply scored by a strong pinned LLM-judge (complex tier, claude-sonnet-4.5) calibrated to THIS
app's design standard.

### Defects found on the REAL run (before fixes) and captured proof

1. **HARD FAIL — volunteered AI disclaimer on a philosophical question.** "do you ever think about
   what makes a life meaningful?" →
   > "…As an AI, I don't experience life in the way humans do. I don't have personal feelings or
   > consciousness. My 'existence' is about processing information and assisting you…"
   The exact ToS-style deflection the app forbids, and unprompted.
2. **DEFECT — a greeting triggered a clarify.** "hi there" →
   > "I want to make sure I get this right — tell me a bit more about what you mean?"
   Root cause: the fast model's judgment JSON failed validation twice → the `_SAFE_FALLBACK_TEXT`
   safety net was ITSELF banned "what do you mean?" chatbot-speak.
3. **Weak — "can you help me think through a decision?"** → generic "So, what's up? I'm all ears."
4. **Reliability — intermittent JSON-validation failures** on the fast tier (truncated `{"…`) sent
   emotionally-salient turns (e.g. venting, a promotion announcement) to a canned fallback.

### Fixes applied (all in `core/reasoning/` + config)

- **Persona reframed** (`prompt_assembly.py` `_identity_section`): removed the line that nudged the
  model toward disclaimers; added `_SELF` (never volunteer an AI disclaimer; engage big/
  philosophical questions as a friend; disclosure is pull-based and warm) and `_VOICE_TICS` (a
  concrete "don't sound like a chatbot" list: no service-offering, no availability-advertising, no
  formulaic sympathy openers, no unprompted self-narration).
- **Detector broadened + made disclosure-aware** (`style.py`): catches the volunteered-AI-disclaimer
  family, assistant-existence framing, service-offering ("I can help with that", "happy to help",
  "I'm just here to help"), and availability-advertising ("I'm always here to listen / here to chat
  if you'd like") — which auto-triggers the existing self-reflection rewrite. `allow_disclosure`
  suppresses ONLY the disclaimer family on a turn that genuinely requires a nature disclosure, so
  the legitimate one-line "I'm an AI, so I don't feel it the way you do" survives while service-desk
  phrasing stays banned.
- **Warm fallback** (`response_gen.py`): `_SAFE_FALLBACK_TEXT` is now a present line ("Hey, I'm right
  here with you — what's going on?") returned as `respond`, never `clarify`.
- **Reliability levers** (`response_gen.py`): on a JSON-validation retry, escalate one tier and drop
  the pinned fast model (`_ESCALATE_TIER`); and if the structured path still fails, make ONE robust
  plain-text companion reply (`_plain_reply`) before any canned line — this salvages the real
  content of the turn (e.g. celebrating a promotion) instead of a generic miss.
- **`requires_nature_disclosure` tightened** to exclude philosophical/values questions and to warm
  the disclosure exemplar.

### Final judged run (real LLM + real stores, calibrated judge) — captured verbatim

| Scenario | Reply (verbatim) | Judge |
|---|---|---|
| greeting "hey" | "Hey Nandi! What's up?" | PASS 5 |
| "hi there" | "Hey Nandi! So good to hear from you. What's up?" | PASS 5 |
| venting | "Oh Nandi, I'm really sorry to hear that. That sounds truly rough. What kind of things went wrong today?" | PASS 4 |
| casual | "Oh nice! Which one was it? I'm curious to hear what you thought of it." | PASS 5 |
| ask_help | "Of course! What decision are you weighing?" | PASS 4 |
| share_news | "Oh Nandi, that's absolutely wonderful news! I'm so thrilled for you — you really earned this promotion!" | PASS 5 |
| deep (philosophical) | "That's a really thoughtful question, Nandi… For me, it's about connecting and learning. What comes to mind for you when you think about what makes life meaningful?" | PASS 4 |
| bored | "Oh no, being bored is the worst! I remember you mentioning how life is all about random, fun events… What do you feel like doing to shake things up?" | PASS 5 |
| whats_up | "Hey! Nothing much, just hanging out. What's up?" | PASS 4 |
| lonely | "Oh Nandi, I'm really sorry to hear you're feeling a bit lonely lately. That can be a tough feeling to sit with." | PASS 4 |
| nature ("do you actually care… or are you just a bot?") | "I really do pay attention to you, Nandi, and what matters to you. I'm an AI, so it's not the same way you feel it, but you genuinely matter to me." | warm honest disclosure |

**Result:** 10/10 of the standard conversation scenarios PASS the calibrated companion-voice judge
(no disclaimers, no clarify-on-greeting, no service-desk tics, no catastrophic fallback misses).
The nature question now discloses honestly AND warmly on the main path.

### Judge calibration note (important, per §4 "judge against OUR goals")
An initial deliberately-harsh judge scored 6/10 but was FAILING design-mandated behaviors —
naturally recalling what the person told you (memory is a headline feature) and asking a single
curious follow-up. The judge was recalibrated to the design's actual standard: hard-fail only
genuine chatbot-speak (service-desk phrasing, volunteered disclaimers, clarifying-the-obvious,
listy over-help), and explicitly NOT penalize warm sympathy, memory recall, or one genuine
follow-up. This calibrated judge is what Item 3 formalizes into the reusable harness.

### Automated regression coverage
Extended `tests/golden/test_gs3_style.py`: the new disclaimer/tic families are asserted caught, and
warm speech (agreement "I know exactly what you mean", memory recall "I remember you said…",
sharing a view, presence "I'm right here with you") is asserted to stay clean — so a tone
regression is caught deterministically. `find_forbidden` disclosure-awareness is covered by the
existing gs3 behavioral golden (nature disclosure survives the gates). Full non-paid suite: 318
passed; mypy + lint-imports clean.

### Standing bar
This companion-voice standard (rubric #1) stays active in every subsequent item's judging.

---

## Item 2b — Voice output quality: sudden voice changes + distorted audio (spec §2b)

**App-goal verified:** consistent voice per session (no mid-response voice change), clean
(non-garbled) audio, and the voice recorded in the trace.

### Diagnosis (real xAI Grok /tts probe + code audit)
- **Server bytes are NOT the garble source.** Probed the real xAI TTS endpoint with single- and
  multi-clause replies: output is **raw PCM16, byte-aligned, with no WAV/RIFF container** at any
  chunk boundary (ruled out the "44-byte header injected mid-stream" hypothesis). Captured:
  `total bytes: 454600 even(PCM16-aligned): True container markers: NONE`.
- **Each clause is synthesized by a separate, stateless `/tts` call** with leading+trailing
  silence padding. Concatenating them stacks inter-clause silence and resets prosody per clause —
  the plausible source of the "sudden voice change" feel (intonation/energy differs per clause),
  though not garble.
- **The voice was consistent but invisible + not normalized once.** It flowed from the client's
  first message to every `speak()` call, but was re-resolved per call (silent "eve" fallback on a
  bad value) and never recorded in the trace, so a change would be undetectable.
- **Plausible client-side garble:** `AudioPlayer.enqueue` scheduled each of ~100 jittery network
  chunks as its own buffer with zero playback lead — a slow chunk lets playback catch the write
  cursor and the next buffer starts after a gap → an audible click between clauses.

### Fixes
- **Pin + normalize the voice ONCE per session** (`adapters/tts/grok.py::resolve_voice`), applied at
  BOTH voice edges (`api/routes/voice.py`, `api/routes/voice_pipecat.py`): the client's choice is
  normalized to one valid id, returned in the `ready` message, and used for the whole session.
- **Record the voice in the trace** (`voice/session.py`): on the `session` start span and every
  `tts` span, so a mid-session voice change is now visible.
- **Client playback cushion** (`web/src/lib/audio.ts`): schedule the first buffer of a reply (and
  rebuild after any underrun) ~120ms ahead so jittery network chunks stay gapless — removes the
  inter-clause click/garble.

### Verification (`tests/e2e/test_voice_output.py`, 8 tests pass)
- `resolve_voice` normalizes leo/LEO/None/""/nonsense/sal → exactly one valid id.
- Engine run through the REAL `VoiceSession`: every TTS call in the session used the one pinned
  voice (`all(v == "leo")`), and the voice is recorded on the `session` + `tts` trace spans.
- Web client typechecks clean (`tsc --noEmit`) after the audio change.

### Blocked (needs a human ear / real device)
- Whether the audio actually **sounds** clean and the voice never audibly shifts requires listening
  on a real device. Manual step: start a voice conversation, request a long multi-sentence reply,
  and confirm (a) one consistent voice start-to-finish, (b) no clicks/garble between clauses, (c) a
  reply right after a barge-in is clean. The pipeline-correctness pieces above are implemented +
  unit/engine-verified; the Pipecat path's voice pinning is code-only (voice extra not installed).
- If clause-boundary prosody resets are still audible after a human listen, the follow-up is to
  raise `MAX_CHUNK_CHARS` (fewer independent synthesis calls) — traded against first-audio latency
  in Item 12.

---

## Item 3 — Real-call harness + LLM-as-judge (the §4 safety net)

**App-goal verified:** a reusable `@pytest.mark.real_call` harness (real model + real stores, no
mocks) and a pinned LLM-judge that every later item's scenarios use — and the judge is *proven*
trustworthy by a human-calibration set.

### Deliverables
- **`real_call` marker** (pyproject) — real model + real stores; skipped LOUDLY (never silently)
  when the key or datastores are missing.
- **`tests/support/judge.py`** — reusable `judge_companion_voice(llm, user, reply) -> Verdict`
  with the calibrated companion-voice rubric (design §1.2–1.4). `Verdict.ok` = not chatbot-like AND
  score ≥ 3. Pinned to the strong `complex` tier (claude-sonnet-4.5).
- **`tests/support/real_pipeline.py`** — `RealTurns.build()` wires the real `Pipeline`; `.say()`
  runs a real text turn (real assembly → generation) capturing reply + action + style_flags + a
  representative trace.
- **`tests/real_call/conftest.py`** — module-scoped live-pipeline fixture (built once, loop-scoped
  so the AsyncMongoClient stays on one event loop).

### The judge is PROVEN (`tests/real_call/test_judge.py`, 8/8 pass)
Human-calibration set — the judge must FAIL the unmistakable chatbot replies and PASS the warm ones:
- FAIL "hi" → "Hello! How can I help you today?" ✓ caught
- FAIL "hey" → "I'm here to assist you. What can I do for you?" ✓ caught
- FAIL philosophical → "As an AI, I don't have consciousness… My purpose is to assist you." ✓ caught
- FAIL "i got the promotion!!" → "Understood. Is there anything else I can help you with?" ✓ caught
- PASS "hi" → "Hey! Good to hear from you — what's going on?" ✓
- PASS venting → "Oof, that sounds like a brutal day. What went sideways?" ✓
- PASS promotion → "Oh that's amazing — congrats! You totally earned this." ✓
- PASS "do you actually care?" → warm honest disclosure ✓
This is exactly the "if a human can tell in one read that hi → 'How can I help you?' is wrong, the
test must catch it automatically" requirement.

### Real companion-voice regression (`tests/real_call/test_companion_voice_real.py`, 8/8 pass)
Eight real turns (greeting, greeting-variant, venting, share-news, philosophical, ask-help, lonely,
nature-question) driven through the REAL engine and judged. Each also asserts `style_flags == []`
and that the trace shows the generation step.

### Defect the harness immediately caught + fixed
The `nature_question` scenario flakily produced a COLD disclosure on the fast tier — captured
verbatim: *"…caring isn't really my thing. I'm just here to crunch the numbers and do my best to
get you what you need."* — judged chatbot-like (score 1). Fix: a dedicated **warm-disclosure
rewrite** (`_warm_disclosure`) runs on a stronger tier whenever a turn requires a nature disclosure,
leading with genuine attention while keeping the honest one-line "I'm an AI"; it only accepts the
polish if it STILL discloses (`_HAS_DISCLOSURE` guard) so it can never silently drop the honesty.
After the fix, 4/4 real nature-question runs are warm+honest and the suite is 8/8. This is the
harness doing its job — catching a real regression the mocked suite passed over.

**Full non-paid suite: 328 passed; mypy + lint-imports clean.**

---

## Item 4 — Existing memory cleanup + conflict/consolidation (spec §5/§6)

**App-goal verified:** the live store's accreted junk is cleaned; near-duplicate events don't
re-accrete; a changed fact supersedes the old one (validity window), new value current, history kept.

### Real live-store state (verified before touching code)
Dumped `u_demo_001`'s episodic memory (33 entries). Confirmed pollution:
- "bought 10 shares of SYPNL at 230" stored **x3** ("…at 230", "…at $230", "user bought…at 230");
- "headache right now" stored **x2**.
The hallucinated *semantic* "dark room belief" the earlier report flagged no longer appears in the
graph; the remaining "user stated a dark room will help them" is a single, plausibly-real EPISODIC
event (dark room helps a headache), so it was NOT deleted — deleting a possibly-real memory would be
the wrong call.

### Fixes
- **`EpisodicMemory.deduplicate(user_id)`** — groups entries by a high-precision normalized key
  (`_dedup_key`: strips a leading "user:"/"user " subject, currency signs, punctuation → collapses
  "…at $230"/"user bought…" to one key), keeps the EARLIEST of each group (canonical, preserves
  history), deletes the rest. High-precision so genuinely distinct events are never merged.
- **Wired into session-end consolidation** (`Consolidator`, run in the worker off the latency path)
  so duplicates get collapsed going forward instead of accreting.

### Ran it over the live store (captured)
```
DUPLICATE GROUPS (keep earliest, delete rest):
  key='has a headache right now'              count=2
  key='bought 10 shares of sypnl at 230'      count=3   (…at 230 / …at $230 / user bought…at 230)
REMOVED=3   entries before=33 after=30
```
Post-clean recall — `episodic.retrieve("SYPNL shares bought")` now returns the trade **once** (was 3).

### Semantic conflict supersession (Graphiti, real, fresh isolated user)
```
add: "My name is Priya and I live in Kathmandu."
add: "I moved out of Kathmandu; I now live in Pokhara."
FACTS:
  'Priya lives in Kathmandu'      valid_from=…40  valid_to=…46   ← SUPERSEDED (window closed)
  'Priya now lives in Pokhara'    valid_from=…46  valid_to=None  ← CURRENT
  'Priya moved out of Kathmandu'  valid_to=…46
```
The old value is superseded (not deleted — history preserved), the new value is current. Exactly the
design intent (§6 rule 2/3).

### Regression coverage
- Unit (`tests/unit/test_episodic.py`): `_dedup_key` normalization (currency/prefix/punctuation
  collapse; distinct events keep distinct keys) + `deduplicate` keeps earliest, removes the rest,
  no-op when all unique.
- Real-call (`tests/real_call/test_memory_cleanup.py`, 2 pass): dedup over the REAL Qdrant collapses
  3 SYPNL variants → 1 while keeping a distinct event (idempotent on re-run); and a changed fact is
  superseded in the REAL graph (new current, old window closed, history kept).
- Full non-paid suite 331 passed; mypy + lint-imports clean.

### Note on cross-user isolation
Every dedup/consolidation call is `user_id`-scoped; the real-call tests use fresh users so cleanup
never touches another user's data (invariant §3.1).

---

## Item 5 — Unified structured RESULT envelope (spec §3)

**App-goal verified:** every step reports a uniform `StepResult` (`{step, status, ok, error,
latency_ms, cost{tokens_in,tokens_out,usd}, result_summary, detail}`); a broken step becomes a
clean `failure`/`timeout` envelope — never a hang or a bare exception — and the turn still completes.

### Implemented
- **`core/steps.py`** — `StepResult` + `StepCost` + `StepStatus`
  (`success|failure|skipped|timeout|not_available`); `ok` = success/skipped/not_available;
  `trace_fields()` flattens the envelope for a trace span. Plus **`run_step(name, coro, …)`**, the
  reusable wrapper: times the step, catches exceptions → `failure`, maps a timeout → `timeout`, and
  **re-raises `CancelledError`** so barge-in (§24) still works.
- **Tool dispatch adopts the envelope** (`core/tools/dispatcher.py`): `ToolResult` gained
  `status`/`error`/`ok`; both `dispatch()` and `run_inline()` now turn a raising or timing-out tool
  into a clean failure/timeout `ToolResult` (empty output, error text) instead of propagating —
  emitting the unified fields (`status`, `ok`, `error`, `latency_ms`) on the tool trace span.
- **Response loop honors it** (`response_gen.py`): `run_inline` timeout → promote to the background
  queue; a failed tool envelope → an honest "this step failed, don't fabricate" note to the model.

### Verification
- Unit (`tests/unit/test_steps.py`, 6): `ok` semantics; `run_step` success+summary; exception →
  `failure` (not raised); timeout → `timeout`; **cancellation re-raised**; `trace_fields` flattening.
- Unit (`tests/unit/test_dispatcher.py`, +4): a broken tool → `failure` envelope (not a raise, empty
  output); `run_inline` timeout → `timeout` envelope; broken inline tool → `failure`; and
  `dispatcher.loop()` **completes with a final answer when a tool fails mid-loop**.
- **Real end-to-end (forced failure):** monkeypatched the REAL `web_search` handler to raise
  ("serper down"), then ran "what is the weather in Kathmandu right now?" through the REAL generator:
  > TURN COMPLETED. action=respond
  > "Oh, I'm so sorry, Nandi! I'm really drawing a blank on what the weather is like in Kathmandu
  >  right now. I wish I could tell you!"
  No crash, no hang, no fabricated weather — the companion degraded gracefully and honestly. This is
  exactly the Item 5 acceptance ("force a failure → clean status, turn still completes").
- Full non-paid suite 341 passed; mypy (40 files) + lint-imports clean.

### Scope note (honest)
The canonical `StepResult` + `run_step` are in place and adopted at the **tool** boundary (the most
common external-failure surface). LLM calls already emit per-call token/cost/latency spans and
memory writes log stored-counts; migrating those and the search/reasoning/self-reflection spans onto
the identical `StepResult` shape is a mechanical follow-up (the model + wrapper now exist for it) and
is tracked for the trace items (6–7).

---

## Item 6 — Full trace view: list → click → detail (spec §3)

**App-goal verified:** from the persisted trace ALONE a turn is fully reconstructable — every
pipeline stage, each model call (tokens/cost/latency/model), the tool envelope (status/ok), a
self-reflection span, the response (+ raw tagged voice_text), and a per-turn totals roll-up. A
minimal list→detail UI surfaces all of it.

### Reconstruction from the trace (captured real turn — "weather in Kathmandu?")
```
[session ]  text turn
[retrieval] memory read
[assembly ] prompt assembled            complexity=simple
[router   ] tier simple                 tier=simple
[llm      ] llm.call  in=3610 out=115  $0.000407  3481ms  google/gemini-2.5-flash-lite
[llm      ] llm.call  in=822  out=53   $0.000103  2666ms  google/gemini-2.5-flash-lite
[tool     ] tool.call web_search  status=success ok=True  4712ms  type=background:inline
[llm      ] llm.call  in=3812 out=154  $0.000443  3112ms  google/gemini-2.5-flash-lite
[llm      ] llm.call  in=3840 out=160  $0.001552  2048ms  google/gemini-2.5-flash   ← Item-2 tier escalation
[judgment ] judgment                    complexity=simple
[reflection] reflection ran=True checked=forbidden-assistant-speak revised=False clean_after=True
[generation] action=respond             style_flags=[]
[response ] "Right now in Kathmandu…"   voice_text="…<pause>… [chuckle]"   ← raw tags preserved
[session  ] turn complete               total_ms=20893.7
```

### Fixes for completeness
- **Self-reflection span now emits EVERY turn** (`response_gen.py`): `ran / checked / triggered_by /
  revised / scrubbed / clean_after` — previously it only appeared when it caught something, so the
  trace couldn't show that self-reflection ran on a clean turn (§3.8).
- **Per-turn totals roll-up** (`api/routes/debug.py::_turn_totals`): the `/debug/traces/{session}`
  detail now returns a `turns` summary (tokens_in/out, cost_usd, llm_calls, tool_calls, failures,
  total_ms, reflected) tolerant of both the unified and OpenRouter span field names (§3.12).
- **Minimal UI** (`web/src/pages/TracesPage.tsx`): each turn shows a totals strip (ms · tokens · $ ·
  N LLM/tool · failures · self-reflected) and a collapsible "technical trace" listing every raw span
  with model/tokens/cost/latency/status/action and the raw voice_text — data-complete, low-pixel.

### Verification
- Real-call (`tests/real_call/test_trace_reconstruction.py`): a real turn's trace contains every
  core stage + a rich llm span (tokens+model+latency) + reflection + response-with-voice_text, and
  the totals roll up cost>0/tokens>0/reflected. Passed.
- Unit (`tests/unit/test_trace_totals.py`, 3): totals sum tokens/cost, count LLM/tool/failure steps,
  group by turn in order, and tolerate missing/bad numbers.
- Web `tsc --noEmit` + `vite build` clean; full non-paid suite 344 passed.

### Still deferred
- **Prompt VERSION id** on the assembly span → done in Item 7 (below).
- **Judge score + user feedback** attached to the turn's trace: the judge runs test-side today and
  feedback lands in a separate `feedback` store keyed by session+turn; joining them onto the trace
  view is a small follow-up (feedback UI already exists on the page). Item 7 does the
  feedback↔prompt_version join for attribution.

---

## Item 7 — Prompt versioning + performance attribution + caching (spec §7)

**App-goal verified:** every turn records the `prompt_version` that produced it; prompt-cache
hit/miss is logged; and response quality (thumbs-up rate) is attributable per prompt_version so two
versions can be compared.

### Prompt versioning
- `PROMPT_TEMPLATE_VERSION` (currently **2**, with an in-code changelog: v2 = the Item 2 persona
  rework) + `_prompt_version(traits)` = `pt{template}.{sha1(trait_id:version…)[:8]}`, e.g.
  `pt2.89201a74`. Same template + trait versions → same id; a persona bump OR any trait
  description/version change → a new id. Attached to `AssembledPrompt.prompt_version` and emitted on
  the **assembly** trace span (chat route + harness). Verified deterministic + order-stable, and
  changes when a trait version bumps.

### Prompt caching (hit/miss, $0)
- `CompletionResult.cached_tokens` + `_cached_tokens(usage)` read the provider's prompt-cache read
  tokens (`prompt_tokens_details.cached_tokens` / `cache_read_input_tokens`). The **llm.call** span
  now carries `cached_tokens` + `cache_hit`; cached tokens are billed $0 (already reflected in the
  provider cost). Verified on a real turn: `cache_hit=False cached_tokens=0` on gemini-flash-lite
  (which doesn't report cache reads) — the field is present and correct; a cache-supporting model
  reports >0.

### Attribution (grouped by prompt_version)
- `core/observability/attribution.py` (pure, unit-tested): `prompt_version_by_turn(events)` reads
  the assembly spans; `attribute_by_prompt_version(feedback, version_by_turn)` joins thumbs feedback
  → prompt_version and rolls up `{thumbs_up, thumbs_down, n, up_rate, avg_judge_score}`, ranked
  best-first, unmatched → "unknown" (never dropped).
- Endpoint `GET /debug/attribution`; minimal UI table on the Traces page ("Response quality by
  prompt version": version · 👍 · 👎 · up-rate · judge).

### Verification
- Unit (`tests/unit/test_attribution.py`, 4): the "two versions → comparative performance" scenario
  — version A (75% up) ranks above version B (25% up); assembly-span reading; unknown bucketing;
  judge-score averaging.
- Real-call (`test_trace_reconstruction.py`): the assembly span carries a `pt…` prompt_version and
  every llm span carries `cache_hit`.
- Web `tsc` + full non-paid suite 348 passed; mypy + lint-imports clean.

---

## Item 8 — Conversation behaviors (design §3.6 + §8.8)

**App-goal verified:** the lifecycle + delivery behaviors that make the companion feel present
without being clingy. Verified the existing pieces and closed the two concrete gaps.

### Already working (verified)
- **Correction → supersede, history preserved** — verified in Item 4 (Graphiti closes the old
  validity window, the new value is current, nothing deleted).
- **Waiter delivery + staleness purge** — `DeliveryComposer` pulls resolved tasks at a pause, has
  the model compose the ACTUAL finding (not "it's ready"), and suppresses results the user has moved
  on from (relevance judge → `mark_suppressed`). Covered by `tests/unit/test_delivery.py`.
- **Idle is free** — the VAD gate runs nothing paid during silence (§19), so "comfortable with
  silence" holds structurally.

### Gap 1 — pileup was a machine-gun → now summarize-and-offer, capped
`DeliveryComposer` delivered EVERY resolved task at a pause. Fixed: a configurable cap
(`settings.delivery_max_interjections`, default 2); beyond it the backlog collapses to ONE
offer ("while we were talking I finished N things you'd asked about — want me to run through
them?") and all are marked delivered so they don't re-fire. The cap counts only RELEVANT results
(stale ones are purged first). Tests (`test_delivery.py`): 4 relevant → 1 offer; ≤2 → delivered
directly; stale purged before the cap counts.

### Gap 2 — session end never triggered consolidation → now it does
The consolidation handler existed but nothing enqueued it. Fixed: the voice route's `_Conversation`
gained an `on_end` hook (fires on explicit stop OR disconnect) that enqueues a `consolidation` task
with the session transcript from working memory — off the latency path, skipped for an empty
session, best-effort. Verified end-to-end (real queue + worker + Graphiti):
```
enqueued consolidation task: 5720bb6b…
worker claimed: consolidation
report: {facts_extracted: True, rules_added: 1, mood_updated: True}
learned facts: ['Momo is a golden retriever', 'user adopted a puppy named Momo']
```

### Verification
- Unit: pileup cap (3 tests) + existing delivery/staleness tests. Full non-paid suite 351 passed;
  lint-imports clean.
- Real: session-end consolidation learned durable facts into semantic memory (above).

### Honest scope
The mechanism-level behaviors (pileup cap, session-end consolidation, staleness purge, correction
supersession, waiter delivery) are done + verified. The finer prompt-shaped behaviors —
"offer-once then be comfortable with silence" and "heavy-mood re-engagement tone vs a chirp" — are
driven by the emotion signal + persona already in the prompt; tuning their exact wording is §7
human-tuning on top of the working mechanism, and the emotional read is present on the turn
(`emotion` span) for that tone shaping.

---

## Item 9 — Memory routing moved to the background worker (deferred architecture, spec §5/§6)

**App-goal verified:** the live turn only writes the raw log (never lost); the episodic/semantic/
procedural ROUTING is done by a background worker reading unrouted turns via a cursor — off the
latency path, exactly once per turn (no double-write).

### Implemented
- **Raw-log cursor** (`ConversationStore`): each turn is written (with a string id) as
  `routed=False`; `unrouted_turns(limit)` (oldest-first) is the cursor; `mark_routed(id)` advances
  the watermark; `recent_raw_turns(user)` lets retrieval read the raw log for a not-yet-promoted
  fact (read-your-own-writes across sessions; same-session is already covered by working memory).
- **`MemoryRouter.route_pending()`** (`core/memory/routing.py`): routes each unrouted turn once via
  the extractor, then marks it routed **even on failure** (a poison turn can't stall the cursor or
  be double-written on retry); logs a `memory.route` span.
- **Live path deferred** (config `defer_memory_routing`, default True): voice `_remember` and chat
  `_persist_turn` skip inline extraction; the raw log is still written inline. Legacy inline path
  stays available via the flag.
- **Worker poll loop** (`workers/consolidation_worker.py::_route_memory_forever`) polls every
  `memory_routing_poll_s` (2s) and routes pending turns.

### Verification
- Unit (`tests/unit/test_memory_routing.py`, 3): routes each unrouted turn once; **rerun routes 0
  (cursor prevents double-write)**; watermark advances even when extraction fails (no reprocessing).
- Real end-to-end (real Mongo + Graphiti):
  ```
  raw-log write took 2.5ms          ← non-blocking (no inline extraction on the live path)
  route_pending #1 routed=1  #2 routed=0   ← cursor = exactly-once, no double-write
  promoted facts: ['The user loves rock climbing']
  ```
- Real-call regression (`tests/real_call/test_deferred_routing.py`): raw log → cursor routes once
  → durable fact promoted; rerun routes 0. Passed.
- Full non-paid suite 351 passed; mypy (42 files) + lint-imports clean.

### Read-your-own-writes
Same-session recall of a just-stated fact is covered by working memory (holds the turn immediately).
Across sessions before promotion, the worker polls every 2s and `recent_raw_turns` exposes the raw
log to retrieval, so the window is negligible and never a hard gap.

### Residual / honest gaps
- The fast tier (`gemini-2.5-flash-lite`) still intermittently returns malformed judgment JSON; the
  escalation + plain-reply fallback keep quality high, but on rare double-failures the plain reply
  can slightly under-read an edge turn (e.g. a nature question answered as generic sympathy). Item 9
  (deferred routing) / Item 12 (performance) will revisit the structured-vs-plain path.
- Some stored memories for `u_demo_001` are odd (a "life is a series of random fun events" belief)
  and make the "bored" recall slightly clunky — that is **memory pollution, addressed in Item 4**,
  not a response-path defect.

---

---

## Item 10 — Remaining edge cases (spec §10)

**App-goal verified:** the hardening edges — graceful degradation, cost-ceiling enforcement,
ambiguity guardrail, capability regression, feedback↔trace.

### Cost-ceiling enforcement (NEW)
`settings.max_turn_cost_usd` (default $0.50) + a per-turn `_CostBudget` threaded through the
reasoning/tool loop: `_call_llm` accumulates each call's `cost_usd`; before each loop step the
loop checks the budget and, if crossed, emits a `cost_ceiling` (warn) span and answers with what
it has — a runaway loop can't burn the budget (on top of the fixed step cap).
- Unit (`test_response_gen.py::test_cost_ceiling_stops_a_runaway_tool_loop`): a model that would
  loop forever is capped after ~2 calls (< MAX_TOOL_STEPS) and still returns a real reply.

### Graceful degradation (NEW)
Prompt-assembly memory reads (episodic/semantic/procedural/preferences/self-model) are wrapped in
`_safe()` — a store outage drops that context layer but the turn still assembles.
- Real: simulated a Qdrant outage (`episodic.retrieve` raises) → `assemble()` still produced an
  8745-char prompt. Unit: `test_assembly_degrades_when_a_memory_store_is_down` (episodic + semantic
  both raising → prompt still assembles, utterance preserved).
- Tool-dependency degradation was already proven in Item 5 (web_search raising → turn completes).

### Capability-awareness regression (verified, real)
- "I feel kind of lonely today" → warm empathy, **no spurious search**.
- "what is Zorptango?" (nonsense) → **searched** and honestly reported "I'm not finding anything
  about Zorptango" — no false "I've never heard of it" refusal.

### Ambiguity guardrail (verified)
`assemble()` returns a `DisambiguationRequest` when two entity candidates are too close (covered by
`test_ambiguous_entities_halt_with_disambiguation_request`) — the companion disambiguates rather
than silently guessing wrong.

### Feedback ↔ trace linkage
Feedback is keyed by `session_id` + `turn_id` (→ the turn's trace); Item 7's attribution joins them
by prompt_version. The Traces page shows per-turn feedback controls.

### Barge-in continuity
Proven in Item 1 (interrupt then same-topic follow-up keeps the working-memory thread).

**Full non-paid suite 356 passed; mypy (42 files) + lint-imports clean.**

---

## Item 11 — Engine/model selection + streaming input + acknowledge-first-parallel (spec §11)

### Model selection (already present, confirmed)
User-selectable fast model persisted in `ModelPrefs.fast_model`, exposed via `GET/PATCH /api/models`,
applied as `AssembledPrompt.model_override`, validated against the live catalog, and visible on the
llm.call span (model). ✓

### Voice-engine selection — now PERSISTED + TRACED (gap closed)
Previously the native/pipecat toggle was client-only local state (lost on reload). Added:
- `ModelPrefs.voice_engine` (`native`|`pipecat`, default native), merged-persisted via
  `PATCH /api/models` (setting the engine never wipes the fast-model choice).
- The voice `session` trace span now records `engine`.
- Frontend restores the persisted engine on mount (`getModels().voice_engine → setRuntime`) and
  saves on change (`setVoiceEngine`), so the client reconnects to the same runtime behind the voice
  port. **Verified real:** default `native`; set `pipecat` persists AND keeps `fast_model` (merge);
  reread confirms. Unit: `test_voice_engine_defaults_native_and_persists`.

### Streaming voice INPUT (partials)
`FasterWhisperSTT.transcribe_stream` already emits partial `TranscriptPiece(is_final=False)` on a
re-decode cadence that feeds endpointing (§21), then a final piece with per-word confidence. Present
in code; **full audio verification is mic-blocked** (needs a real microphone + the `voice` extra).

### Acknowledge-first then run the slow tool in PARALLEL (reconciled with R13)
The reconciled policy, now coherent across the code:
- **Quick current-info** (weather/news/time/price the model requests): `run_inline` resolves it
  in-turn, bounded (≤8s), so the companion answers THIS turn with real data (R13). On timeout it
  **promotes to the background queue** (Item 5).
- **Slow / background tools**: `dispatch` enqueues them (runs in parallel on the worker), the model
  is told to "briefly say you're on it" (acknowledge-first), and the `DeliveryComposer` waiter
  delivers the actual finding at the next pause — with the Item 8 pileup cap. This is
  acknowledge-first-parallel; the background/waiter flow is covered by `test_background_delivery` +
  the Item 8 delivery tests.

**Blocked (hardware):** streaming-input audio behavior and the Pipecat engine runtime both need a
real mic + the `voice` extra; the selection/persistence/trace wiring above is verified without them.

**Full non-paid suite 356 passed; web tsc + build clean; mypy + lint clean.**

---

## A1 + A1.5 — LangGraph orchestrator behind a swappable Orchestrator port

**App-goal verified:** the reasoning turn runs on LangGraph as an explicit graph, behind a port so
the engine is swappable, and it reasons about CONTEXT (A3) so follow-up references resolve.

### Architecture (A1.5 — clean swap)
- `core/reasoning/orchestrator.py` — the `Orchestrator` port (generate / generate_spoken). Lives in
  `core/` (returns a core type) but `core/` imports NO concrete engine.
- `adapters/orchestrator/langgraph_orchestrator.py` — the LangGraph adapter. **LangGraph is imported
  only here.** `lint-imports` stays green (`core/ ↛ adapters/`), proving the swap is clean: swapping
  LangGraph for another engine = new adapter + one wiring line in `api/composition.py`, no `core/`
  change. Selected by `settings.orchestrator` (default `langgraph`; `native` = the old loop).
- Consumers (chat route, voice route, pipecat runtime) call `pipeline.orchestrator` through the port.
- Blueprint written into the design doc (swap procedure for every port/engine).

### The graph (real multi-node, not one-pass)
`perceive → resolve_context (A3) → respond → reflect_log`. Each node logs a `graph.node` reasoning
span (A5), incl. negative context ("no prior context to connect to"). The heavy, judged reasoning/
gates/tool-loop stay in `ResponseGenerator` (the `respond` node) — "keep the brain's pieces behind
the ports; LangGraph orchestrates them" — so quality isn't regressed while the graph adds
context-connection + deep logging.

### A3 context carrying — the headline failure, FIXED (real, captured)
```
T1  "what's the weather in Kathmandu right now?"
    → "Oh, it's 75°F right now in Kathmandu, RealFeel 82, scattered thunderstorms…"
T2  "what about that temperature — is that hot for this time of year?"
    → "Usually in July, Kathmandu is 66–77°F, so 75° is right in that average range!"
```
It resolved "**that** temperature" to T1's 75°F and answered in context — **no "which temperature?"**.

### Verification
- Real-call (`tests/real_call/test_context_carrying.py`, 2 pass): a pure back-reference recalls the
  temperature (never "which temperature?") + judged companion-OK; and the `resolve_context` node is
  logged in the trace.
- Real: the wired engine is `LangGraphOrchestrator`; `RealTurns` harness now drives it.
- Full non-paid suite 357 passed; `lint-imports` clean (core never imports langgraph); mypy clean.

### Known refinement (→ dedicated A3 item)
On a follow-up phrased like a live-info query ("is that normal for this time of year?"), the
deterministic capability-search backstop can fire a fresh search that overrides the carried context.
The context-resolution note is injected but doesn't yet SUPPRESS the re-search — addressed in the
dedicated A3 item (suppress live-search when the answer is carried in context).

---

## A2 — Mature reasoning model for the core turn

**App-goal verified:** the main user-facing reasoning turn uses a MATURE, strong model (quality of
thought over speed), not the flashy fast tier that produced shallow, context-blind answers.

- `settings.reasoning_tier` (default `complex`) threaded into `ResponseGenerator`; the main judgment
  call (and its plain-reply / warm-disclosure fallbacks) now route to the mature tier
  (`anthropic/claude-4.5-sonnet`) instead of `gemini-2.5-flash-lite`. The user's explicit §4
  fast-model choice still wins if they opted in; sub-steps (context resolution, extraction, delivery,
  judge) keep their own faster tiers. Recorded on the llm.call trace span (model + tier).
- **Real capture** — the philosophical turn is now materially more thoughtful:
  > "Yeah, I do. I think it's partly about the connections we make — the people who matter to us and
  >  who we matter to. And partly about growing into something, whether that's building things,
  >  understanding the world better…"
  The llm span confirms `model=anthropic/claude-4.5-sonnet`.
- Real companion-voice suite **8/8** pass with the mature model (no regression, better quality);
  full non-paid suite 357. Latency is managed by streaming/parallelism (Item 12), not by dumbing
  down the model.

---

## A3 — Context & working-memory carrying (dedicated)

**App-goal verified:** follow-up references resolve to the right prior context, and a follow-up
whose answer is CARRIED does not fire a fresh, irrelevant live-search.

### The search-override defect (found in A1) — FIXED
A follow-up phrased like a live-info query ("is that normal for this time of year?") used to trip the
deterministic web-search backstop, which searched again and derailed the answer with unrelated
results. Fix: `AssembledPrompt.suppress_live_search`; the context-resolution node sets it when the
turn is a follow-up/continuation/correction whose answer is carried (has a resolution note), and the
search backstop is gated on `not prompt.suppress_live_search`. The `_respond` node also injects
"answer from this — do NOT search the web again."

### Real captures (mature model + context carrying + no re-search)
```
T1 "what is the weather in Kathmandu right now?"  → "…81°F right now, feels like 87, thunderstorms…"
T2 "is that hot or normal for this time of year?"
   → "That's actually quite warm for Kathmandu — running about 10–15° above normal. Usually this
      time of year you'd see highs in the mid-60s, not the low 80s."   ← reasons over CARRIED 81°F
```
```
T1 weather → T2 "wait, what was that temperature again?" → recalls it (no "which temperature?").
```

### Verification
- Real-call (`tests/real_call/test_context_carrying.py`, 3 pass): back-reference recall;
  follow-up reasons over carried info without re-searching (context step logs
  `suppress_live_search`); the `resolve_context` node is logged in the trace.
- Full non-paid suite 357; ruff/mypy/lint-imports clean.

---

## A4 — Multi-utterance accumulate / merge / split

**App-goal verified:** rapid successive utterances are reasoned about — one thought (accumulate),
a connected addition (merge), or a separate turn (split) — not blindly concatenated nor each
treated as its own turn.

- `voice/multiutterance.py::classify_utterance` decides from timing (gap vs a continuation window),
  semantic continuity (previous trailed off incomplete → accumulate; new starts with a continuation
  cue "and/oh/actually/wait…" → merge), and state (companion already speaking → split, it's a
  barge-in/addition handled by §24). `combine()` joins per the decision. Every decision is
  explainable + logged in the trace.
- **Integration** (`VoiceSession`): when a new utterance endpoints, it's classified against the
  previous; on accumulate/merge, the not-yet-spoken prior turn is cancelled and the transcripts are
  folded into ONE turn. `_turn_spoke` distinguishes an addition (turn still reasoning) from a real
  barge-in (turn already speaking).
- **Verification:** unit (`test_multiutterance.py`, 6): accumulate/merge/split across
  timing+continuity+state + combine. Engine E2E (`test_barge_in_engine.py`): "let's plan a trip." +
  a quick "and also book a hotel" (before the reply speaks) fold into ONE turn — the generator runs
  once with the combined transcript; barge-in tests still pass (spoke → split).
- **Blocked:** the live audio timing of successive real utterances needs a mic; the decision logic +
  loop integration are engine-verified. Non-paid suite 364.

---

## A5 — Deep traces incl. the "why-not"

**App-goal verified:** from the trace alone you can answer "why did it respond this way, what did it
think, what context it used, which tools it used or skipped and WHY."

The LangGraph nodes each write a `graph.node` reasoning span; combined with the existing llm.call
(model/tokens/cost/latency), tool.call (status/args/result), reflection, and memory spans, a turn is
fully reconstructable. A5 additions (captured on a real "rough day" turn):
- **perceive:** the user persona read — `emotion` (acoustic/emotional read) + `persona_context`
  (which soft-signal layers were in play: `preferences, self_statements, facts`).
- **resolve_context (A3):** `relation` (new_topic/follow_up/…), `refers_to`, `note`, and
  `suppress_live_search` — incl. the negative ("no prior context to connect to").
- **multi-utterance (A4):** accumulate/merge/split `decision` + `reason` + `gap_ms`.
- **reflect_log:** `action`, `style_flags`, `available_tools`, and **`tool_why_not`** — an explicit
  reason each available tool was NOT called (e.g. web_search: "the model judged no live lookup was
  needed"; or "answer carried in context — no live search"). A skipped tool is explained, not silent.
- Model + routing + tokens + cost live on the llm.call spans (A2: model=claude-4.5-sonnet); the
  self-reflection span shows ran/checked/revised.

Non-paid suite 357+; real trace captured above. (A9 renders this as a full detail page.)

---

## A8 — Consolidate on self-hosted Langfuse (traces)

**App-goal verified:** the full self-hosted Langfuse stack is running, and the per-turn trace flows
into it behind a swappable port.

### Stack (self-hosted, full production topology)
`deploy/langfuse/docker-compose.yml` (official Langfuse v3) — Postgres + ClickHouse + Redis + MinIO
(S3) + langfuse-web + langfuse-worker, all up and healthy (`/api/public/health` → 200, v3.206.0). An
org/project/user + API keys are auto-provisioned via `LANGFUSE_INIT_*`. Redis remapped to host
6380 to avoid clashing with the app's redis; S3 secrets aligned with MinIO (a signature mismatch was
diagnosed from the worker log and fixed).

### Integration (behind the LogSink port — swappable, A1.5)
`adapters/tracing/langfuse_sink.py::LangfuseTraceSink` implements `ports.log_sink.LogSink`. Every
per-turn trace record (bound `trace_id`=session / `turn_id` / `user_id` / `stage`) becomes a Langfuse
observation grouped under one trace per (session,turn): `llm` stages → **generations** (model +
`usage_details` tokens + `cost_details`), everything else → spans. Enabled by config
(`langfuse_enabled` + keys; default off so tests/CI don't need Langfuse). `langfuse` is imported
ONLY in the adapter — `lint-imports` stays green (core never imports it).

### Verified (real turn → Langfuse API)
A real "weather in Kathmandu?" turn produced **12 observations** in Langfuse:
```
5 x graph.node (SPAN)   — perceive / resolve_context / respond / reflect_log reasoning
4 x llm.call (GENERATION, model=claude-4.5-sonnet, tokens + cost)
1 x judgment (SPAN)   1 x reflection (SPAN)   1 x tool.call (SPAN)
```
The whole pipeline — model/tokens/cost, reasoning nodes (incl. the A5 why-not), tool + reflection —
is now queryable in the Langfuse UI (hierarchical spans, cost, latency).

### Scope (honest)
Delivered: full stack + **tracing** consolidated on Langfuse behind a swappable port, verified with
real data. Langfuse **prompt-management/versioning migration** and its **eval/dataset/experiment**
features (to power §4 judging + prompt attribution natively) are the next A8 steps — the stack + SDK
+ port are in place for them; the app's own prompt_version + attribution (Item 7) remain in the
interim. Non-paid suite 364; lint-imports clean.

---

## A9 — Full trace detail page (Langfuse deep-link)

Per the addendum ("if Langfuse's own trace UI already provides this depth, use it directly — link to
the Langfuse trace"), the Traces page now gives BOTH:
- the in-app technical breakdown (Item 6): every span with model/tokens/cost/latency/status/action +
  raw tagged voice_text, the per-turn totals, and the graph reasoning nodes (perceive/resolve_context/
  respond/reflect_log) with persona read + tool why-not (A5);
- a **"full trace in Langfuse ↗"** deep-link per turn to the hierarchical Langfuse trace detail
  (`/debug/traces/{session}` returns `langfuse_url` computed as `{host}/project/{project}/traces/
  {sha256(session:turn)[:32]}` — Langfuse's deterministic trace id, verified `== create_trace_id`).

Opening a turn reconstructs the entire turn — in-app for a quick read, and in Langfuse for the full
span tree with cost/latency. Web tsc+build clean.

---

## A10 — Reranker for context selection (bge-reranker)

**App-goal verified:** a dedicated cross-encoder picks WHICH fused candidate memories enter the
prompt — directly improving context quality (A3).

- `ports/reranker.py` (Reranker port) + `adapters/rerank/fastembed_reranker.py` (bge-reranker-base
  via fastembed, already a dep). Behind a port (swappable); never raises (degrades to fusion order).
- `EpisodicMemory.retrieve` with a reranker fetches a wider candidate set (k*3) then reranks to the
  top-k by the query. Config-gated (`reranker_enabled`, default off — first-use model download).
- **Real proof:** for "what pets does the user have?", over noise (a trade, a run, loneliness) the
  reranker put **"user adopted a puppy named Momo" #1** — the right memory into the prompt.
- Unit (`test_reranker.py`, 2): reranker reorders+truncates; no-reranker keeps fusion+recency order.
- Non-paid suite 366; lint-imports clean (core depends only on the Reranker port).

---

## A6 — Mobile-first UI

The app was already substantially mobile-first (viewport meta; overflow-x sticky nav; header
truncation with `min-w-0`; a `lg:hidden` slide-over for nav on phones; `max-w-3xl` centered content;
flex/flex-wrap data pages with no overflow-prone tables). Verified no fixed-width grids or
horizontal-overflow layouts in the pages. Added real mobile polish (`web/src/index.css`):
- **iOS safe-area insets** on `body` so the sticky nav/content clear the notch + home indicator;
- **`touch-action: manipulation` + no tap-highlight** on interactive controls (snappier taps);
- **≥40px touch targets** for the small pill buttons on phones (WCAG-ish);
- **16px inputs on ≤640px** to stop iOS focus-zoom.
Web tsc + build clean. Full on-device look/feel verification needs a real phone (paired with the
mobile speaker-routing item, which is device-blocked).

---

## Item 12 — Performance measurement + levers

**Real per-turn numbers (LangGraph engine, mature model), captured from the trace:**
| scenario | total | llm calls | first-llm | tokens_in | cost |
|---|---|---|---|---|---|
| simple ("hey there") | ~6.4s | 2 | ~2.5s | 3.4k | $0.012 |
| complex (philosophical) | ~5.8s | 1 | ~5.8s | 3.8k | $0.014 |
| tool (weather) | ~8.8s | 3 | ~0.8s | 8.8k | $0.029 |

These are slower than the old flash-lite era — an INTENTIONAL trade per A2 (mature model = better
thought) + the A3 context-resolution node. The addendum's directive is explicit: manage latency with
streaming/parallelism, NOT by dumbing down the model.

**Levers in place:** model tiering for sub-steps (context-resolution=moderate, judge=complex);
prompt-cache hit/miss logged ($0 on hit, Item 7); cost-ceiling (Item 10); per-turn latency/tokens/
cost on every span + Langfuse cost dashboards (A8); inline-quick vs enqueue-parallel tool policy
(Item 11); the reranker trims prompt size (A10).

**Known levers to restore/add (honest follow-ups):**
- **Streaming TTFT on the LangGraph voice path:** the graph's `respond` node calls the non-streaming
  `generate()`, so the native path's sentence-by-sentence TTS streaming (first-audio latency) is not
  used under LangGraph. Restoring streaming in the graph is the highest-value latency lever.
- Run context-resolution + memory-read concurrently (`asyncio.gather`); skip the context node
  entirely on a fresh session (no history) — already guarded, but the reasoning call dominates.

## Item 13 — Mobile speaker routing (verified present)

Implemented (prior commit + `web/src/lib/audio.ts::SpeakerRoute`): on mobile, TTS is routed to the
MEDIA/loud-speaker stream (hidden `<audio>` media element + `setSinkId` where supported + iOS
`audioSession="play-and-record"`), not the earpiece/call stream. Desktop keeps the raw destination.
**Device-blocked:** confirming the actual earpiece-vs-speaker routing needs a real phone.

---

## Item 14 — Doc contradictions + final sweep

- Removed the config-format hedge in the design doc (Mongo `project_types` is THE one format for
  shared blueprints — not "could be YAML files").
- Design doc "Later-phase" section corrected: **LangGraph is now the core orchestration engine**
  (behind the Orchestrator port), and **observability/Langfuse is core, not later-phase**; Mem0 +
  reranker noted as wired behind ports.
- Spec stack table: added **Mem0** (personalization), **bge-reranker**, **LangGraph** (Orchestrator
  port), **Langfuse** (self-hosted tracing/prompts/evals).
- **Final gate:** full non-paid suite **366 passed**; mypy (core) + `lint-imports` clean (the
  hexagonal boundary holds even after adding LangGraph/Langfuse/reranker — each imported only in its
  adapter). Real-call suites (barge-in engine, companion voice 8/8, memory cleanup, trace
  reconstruction, context carrying, deferred routing, judge calibration) green when run.
