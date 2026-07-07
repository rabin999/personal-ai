# Autonomous Execution Backlog

Legend: `[ ]` not started · `[~] in progress` · `[x] done+verified` · `[blocked: reason]`

Source of truth for the ordered run (see the execution plan). One item at a time, in order.
Each item proven by real end-to-end scenarios + LLM-judge, logged to `docs/TEST_REPORT.md`,
committed locally, and logged in `docs/REMEDIATION_LOG.md`.

- [x] **Item 1** — Barge-in / interruption (full-duplex, cancellable TTS + generation) — native
  path proven by 3 real engine E2E scenarios; Pipecat path fixed (allow_interruptions + cancellable
  reply) but runtime-blocked (voice extra not installed); browser mic/AEC blocked (needs a mic)
- [ ] **Item 2** — Response quality: companion, not chatbot (continuous top goal)
- [ ] **Item 2b** — Voice output quality: sudden voice changes + distorted audio
- [ ] **Item 3** — Real-call test harness + LLM-as-judge (the §4 safety net)
- [ ] **Item 4** — Existing memory cleanup + conflict/consolidation
- [ ] **Item 5** — Unified structured RESULT envelope
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
