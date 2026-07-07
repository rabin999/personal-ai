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

### Residual / honest gaps
- The fast tier (`gemini-2.5-flash-lite`) still intermittently returns malformed judgment JSON; the
  escalation + plain-reply fallback keep quality high, but on rare double-failures the plain reply
  can slightly under-read an edge turn (e.g. a nature question answered as generic sympathy). Item 9
  (deferred routing) / Item 12 (performance) will revisit the structured-vs-plain path.
- Some stored memories for `u_demo_001` are odd (a "life is a series of random fun events" belief)
  and make the "bored" recall slightly clunky — that is **memory pollution, addressed in Item 4**,
  not a response-path defect.

---
