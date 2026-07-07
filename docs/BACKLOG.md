# Autonomous Execution Backlog

Legend: `[ ]` not started · `[~] in progress` · `[x] done+verified` · `[blocked: reason]`

Source of truth for the ordered run (see the execution plan). One item at a time, in order.
Each item proven by real end-to-end scenarios + LLM-judge, logged to `docs/TEST_REPORT.md`,
committed locally, and logged in `docs/REMEDIATION_LOG.md`.

- [x] **Item 1** — Barge-in / interruption (full-duplex, cancellable TTS + generation) — native
  path proven by 3 real engine E2E scenarios; Pipecat path fixed (allow_interruptions + cancellable
  reply) but runtime-blocked (voice extra not installed); browser mic/AEC blocked (needs a mic)
- [x] **Item 2** — Response quality: companion, not chatbot — real judged runs 10/10 on the
  calibrated companion-voice judge; fixed volunteered AI disclaimers, clarify-on-greeting,
  service-desk tics, and JSON-failure catastrophic misses (tier escalation + plain-reply salvage).
  Standing bar for all later items.
- [x] **Item 2b** — Voice output quality — voice pinned+normalized once per session + recorded in
  trace; client playback cushion removes inter-clause click; server bytes verified clean raw PCM.
  Audio-by-ear + Pipecat runtime blocked (needs device / voice extra).
- [x] **Item 3** — Real-call harness + LLM-as-judge — `real_call` marker + reusable judge +
  live-pipeline harness; judge PROVEN by an 8/8 calibration set (fails "hi→How can I help you?",
  passes warm replies); real companion suite 8/8; caught+fixed a cold-disclosure regression.
- [x] **Item 4** — Memory cleanup + conflict/consolidation — live store deduped (SYPNL 3→1, headache 2→1); dedup wired into consolidation; Graphiti supersession verified (old superseded, new current, history kept); unit + real_call tests
- [x] **Item 5** — Unified structured RESULT envelope — StepResult + run_step wrapper; tool dispatch produces clean failure/timeout envelopes (never raises); real forced-failure turn completes gracefully; unit + real e2e
- [ ] **Item 6** — Full trace view: list → click → detail (backend-complete; UI minimal)
- [ ] **Item 7** — Prompt versioning + performance attribution + caching
- [ ] **Item 8** — Conversation behaviors (design §3.6 + §8.8)
- [ ] **Item 9** — Memory routing moved to background worker (deferred architecture)
- [ ] **Item 10** — Remaining edge cases (degradation, cost ceiling, ambiguity, feedback→trace)
- [ ] **Item 11** — Engine/model selection + streaming input + acknowledge-first-parallel
- [ ] **Item 12** — Performance testing + latency levers
- [ ] **Item 13** — Mobile speaker routing (native default + Pipecat)
- [ ] **Item 14** — Doc contradictions + FINAL full sweep

**Environment confirmed (session start 2026-07-07):** real stores up (Mongo/Qdrant/Neo4j/Redis
via docker), OpenRouter + Serper + X-AI keys set. Real end-to-end testing is possible.
