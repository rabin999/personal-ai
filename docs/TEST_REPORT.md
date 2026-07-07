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

### Residual / honest gaps
- The fast tier (`gemini-2.5-flash-lite`) still intermittently returns malformed judgment JSON; the
  escalation + plain-reply fallback keep quality high, but on rare double-failures the plain reply
  can slightly under-read an edge turn (e.g. a nature question answered as generic sympathy). Item 9
  (deferred routing) / Item 12 (performance) will revisit the structured-vs-plain path.
- Some stored memories for `u_demo_001` are odd (a "life is a series of random fun events" belief)
  and make the "bored" recall slightly clunky — that is **memory pollution, addressed in Item 4**,
  not a response-path defect.

---
