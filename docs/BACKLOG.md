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
- [x] **Item 6** — Full trace view — turn fully reconstructable from the trace (all stages, LLM tokens/cost/latency, tool envelope, self-reflection span every turn, raw voice_text, per-turn totals roll-up); minimal list→detail UI with a technical-trace breakdown. real_call + unit tests
- [x] **Item 7** — Prompt versioning + attribution + caching — prompt_version (pt2.<traits-hash>) on every assembly span; cache hit/miss on llm spans; /debug/attribution groups thumbs-up rate by prompt_version (+ UI table). two-version comparison unit-tested
- [x] **Item 8** — Conversation behaviors — pileup cap (summarize-and-offer, config-driven, anti-machine-gun) + session-end→consolidation hook (verified: learned durable facts). correction-supersede/staleness/waiter already working+verified
- [x] **Item 9** — Memory routing to background worker — raw-log cursor (routed watermark) + MemoryRouter.route_pending (exactly-once) + worker poll loop; live path deferred (raw-log write 2.5ms, no inline extract). cursor prevents double-write (verified real). config defer_memory_routing
- [x] **Item 10** — Remaining edge cases — cost-ceiling enforcement (per-turn _CostBudget, config cap) + graceful degradation (memory reads wrapped, store-down still assembles) + verified capability regression / ambiguity guardrail / feedback→trace
- [x] **Item 11** — Engine/model selection + streaming + ack-first — voice_engine now persisted (merge-safe) + traced + restored client-side; model selection already done; streaming-input partials exist (mic-blocked); ack-first-parallel reconciled with R13 (inline-quick / enqueue+waiter-slow)
- [ ] **Item 12** — Performance testing + latency levers
- [ ] **Item 13** — Mobile speaker routing (native default + Pipecat)
- [ ] **Item 14** — Doc contradictions + FINAL full sweep

**Environment confirmed (session start 2026-07-07):** real stores up (Mongo/Qdrant/Neo4j/Redis
via docker), OpenRouter + Serper + X-AI keys set. Real end-to-end testing is possible.
- [x] **A1+A1.5** — LangGraph orchestrator behind a swappable Orchestrator port — graph
  (perceive→resolve_context(A3)→respond→reflect_log); core never imports langgraph (lint-imports
  clean = clean swap); A3 context carrying fixed ("that temperature"→prior weather); real_call tests
- [x] **A2** — Mature reasoning model — main turn now routes to claude-4.5-sonnet (settings.reasoning_tier=complex), recorded in trace; sub-steps keep faster tiers; noticeably more thoughtful (real capture); companion suite 8/8
- [x] **A3** — Context/working-memory — suppress_live_search flag set by the context node on a follow-up (no irrelevant re-search); reasons over carried info (real captures); 3 real_call scenarios pass
- [ ] **A4** — Multi-utterance accumulate/merge/split
- [ ] **A5** — Deep traces incl why-not (extend across all nodes)
- [ ] **A8** — Consolidate on Langfuse (self-hosted) for prompt mgmt/evals/traces
- [ ] **A9** — Full trace detail page
- [ ] **A10** — Reranker / RAGAS / Arq / secrets / rate-limit / migrations
- [ ] **A6** — Mobile-first UI
