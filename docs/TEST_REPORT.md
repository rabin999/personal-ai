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
