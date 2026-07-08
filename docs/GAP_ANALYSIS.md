# Gap Analysis — Personal AI Companion (actual running app vs. design doc)

**Method:** evidence-based, not assumed. Every "working" claim below is backed by a REAL run this
round (real OpenRouter models + real Mongo/Qdrant/Neo4j/Redis/Langfuse, no mocks), captured in
`docs/TEST_REPORT.md`, and — where relevant — confirmed visible in the per-turn trace. Where a real
run fails or a piece is unverifiable without hardware/human action, it is marked as such and NOT
claimed done. Source of truth: `docs/ai-companion-design-doc.md` (§0–§19 + the A1.5 Addendum; note:
the doc has **no §20** — the combined brief references a "new §20" that does not exist in the design
doc, so §0–§19 + Addendum are treated as the spec).

Date: 2026-07-08. Branch: main.

---

## 1. Fixes applied (Execution Plan → ADDENDUM → Follow-up → this Combined round)

Full detail with captured scenarios/traces is in `docs/TEST_REPORT.md` / `docs/REMEDIATION_LOG.md`.
This is the roll-up of the **combined critical-fixes round (C1–C11)**, on top of the earlier rounds
(the 26 modules + Application Assembly in `docs/BUILD_STATUS.md`, and fixes F1–F21).

| # | Was broken | What was done | Proven by |
|---|---|---|---|
| **C1** | Trace showed stage labels, not the internal story; no per-call purpose/params; UI truncated prompts at 400 chars | Threaded `purpose`+params+precise start/end timing through the LLM port; labelled all 16 call sites; rewrote the in-app trace detail to show a purpose-badged model-call map (parallel/sequential), expandable verbatim prompts+replies, and full reasoning/reflection/judgment/assembly/tool/memory spans | Real live-search turn: 36 events / 8 labelled LLM calls; strict LLM-judge scored trace-completeness **7/7 = COMPLETE** from the trace alone |
| **C2** | Interrupt cancelled generation but the VOICE KEPT PLAYING | Diagnosed the leak (audio buffered downstream: merge queue → WS/OS buffer → browser playback; client re-enqueued post-barge audio). Server emits the full stop sequence + flushed-chunk count; client `AudioPlayer.interrupt()` stops playback AND mutes so trailing audio is dropped until the next reply's TTS | Real VoiceSession barge-in: audio truncated (101 vs 120 chunks), generation cancelled + TTS closed, two-phase trace; 6 barge-in tests green |
| **C3** | Intent/source routing + follow-up correlation appeared wrong | Fixed a real-call harness trace-fidelity bug (hard-coded `turn_id=1`) that masked correct per-turn behavior; verified the engine already routes internal-vs-live correctly and correlates follow-ups/references | LLM-judge **1.0 PASS**: portfolio LTP→live search, "how many do I have"→internal, bare "and the price?"→SYPNL, garbled/cross-turn references, correct topic-switch |
| **C4 + C5** | Time answers machine-ish + not user-relative; units defaulted to US; user model had NO location/timezone/units | Added `LocaleProfile` to the user; prompt assembly injects the user's local clock time + a general HUMANIZE directive (local time relative to user, unit-leading, currency, paraphrase, concrete-first); `user_context_signals` recorded in the trace | Two users, same question localized differently (Celsius/km vs °F/mi); LLM-judge **1.0 PASS** incl. user-relative time "~4.5 hrs behind you", km-first distance, NPR currency |
| **C6** | Should verify when uncertain/outdated | Verified the existing volatility assessment (`needs_live_info`) + response-gen live-info backstop already do this | LLM-judge: correct **verify-vs-answer 5/5**; app returned current 2026 info where the judge's 2024 memory couldn't |
| **C7** | Voice a bit slow, not configurable | `voice_speed` (default **1.2×**, clamped 0.8–1.5) on the profile; server sends it in `ready`; client applies `playbackRate` to both engines' shared sink; `PATCH /api/prefs` + profile-panel slider | 2.0→clamp 1.5, persists, surfaced in ready payload; web build green |
| **C8** | Branding/contrast/nav | AA-contrast fix on detail labels; confirmed one consistent Shell/header/theme across all routes + mobile-first polish; full `vite build` green | Build+tsc green; **partial** — see §3 (full visual redesign needs an authenticated browser) |
| **C9** | HITL via Langfuse | Verified prompt steering (managed versions, runtime, bundled fallback), feedback→score on the trace, judge+human co-located for calibration, native annotation | Live Langfuse 4.13.1: `context_intent` v4 source=langfuse; dead-host → `source=fallback`; score ingest HTTP 201 |

---

## 2. Implemented & working — VERIFIED this round (what it does + evidence)

- **ReAct reasoning loop (design §14, §17):** perceive → resolve-context/intent → reason → tools →
  observe → respond, as a real LangGraph graph behind the Orchestrator port. *Evidence:* every real
  turn's trace shows the `perceive → resolve_context → respond → reflect_log` nodes with content.
- **Self-reflection before finalizing (design §3/§9):** draft → critique → revise is a first-class
  step. *Evidence:* the `reflection` span carries `draft`, `critique`, `revised`, `revised_text`.
- **Context assembly reads memory before reasoning, writes after (design §4, §14):** working +
  episodic + semantic + procedural + project + psych are assembled; extraction/consolidation writes
  after. *Evidence:* `retrieval` span lists what each store returned; cross-session recall works.
- **Intent + context correlation (design §3, C3):** internal-vs-live source routing, follow-up
  continuity, reference/anaphora resolution, indirect-intent inference. *Evidence:* LLM-judge 1.0.
- **Human-quality humanized answers framed for the user (design §3, C4):** local time relative to the
  user, their units/currency, paraphrased tool output. *Evidence:* LLM-judge 1.0, two-locale test.
- **Knows & uses the user (design §6, §18, C5):** `LocaleProfile` (timezone/city/country/units/
  currency/language) + psychological user-model (§17) shape responses; the trace records
  `user_context_signals`. *Evidence:* signals in the identity block + localized answers per user.
- **Search-when-uncertain (design §8, C6):** volatile/current → search; stable → direct. *Evidence:*
  judge 5/5 correct decisions; live 2026 facts returned.
- **Deep per-turn traces (design §17 tracing, C1):** every LLM call's purpose/params/prompt/reply/
  tokens/cost/latency/cache + ordering + reasoning decisions, displayed in-app. *Evidence:* judge 7/7.
- **Real voice interruption mechanism (design §24, C2):** stop playback + flush queue + drop trailing
  audio + cancel generation. *Evidence:* real barge-in run truncates audio; 6 tests green.
- **Memory stores wired & real (design §4, §9):** Qdrant episodic (dense+BM25+RRF), Mongo doc store,
  Graphiti+Neo4j semantic/temporal, Mem0 personalization, Redis queue/cache. *Evidence:* BUILD_STATUS
  integration + real-call suites; recall turns answer stored facts across sessions.
- **Cost ledger, multi-tenant isolation, ports boundary (invariants §3):** every paid call logs cost;
  every store query is `user_id`-scoped; `lint-imports` proves `core/ !→ adapters/`. *Evidence:*
  contracts "2 kept, 0 broken"; two-user locale test isolates.
- **Langfuse HITL (design §17 tracing, C9):** runtime prompt steering + fallback, feedback scores,
  calibration surface, annotation. *Evidence:* live Langfuse round-trips.
- **Voice runtime (design §11, §19–§24):** continuous turn-taking, VAD cost-gate (idle free),
  semantic endpointing, Grok streaming TTS, one-turn-behind SER. *Evidence:* voice e2e + barge-in
  engine tests.

**Quality gates green this round:** `ruff` clean, `lint-imports` 2/2 kept, `mypy` (prod dirs) clean,
fast pytest green, web `tsc` + `vite build` green.

---

## 3. Partial / broken / not-wired (honest)

- **C8 — UI visual polish (PARTIAL):** structural + accessibility (AA contrast) + consistent-shell +
  mobile-first are code-verified and the build is green, but a full brand/theme REDESIGN with
  cross-viewport pixel QA (Playwright screenshots of the AUTHENTICATED pages) was **not** completed —
  those pages sit behind real Google SSO which I cannot log into unattended. Not fabricated as "looks
  great." *Remaining:* subjective visual sign-off in a real browser.
- **Automated per-turn LLM-judge score (CONFIG-GATED):** the judge that feeds Langfuse calibration is
  behind `LANGFUSE_EVAL_ENABLED` (off by default — it's an extra paid call per turn). Human feedback +
  prompt steering + fallback work regardless; flip the flag to run the judge continuously.
- **SER / acoustic emotion (WIRED, needs GPU to fully exercise):** `emotion2vec` microservice +
  `LaggingEmotionProvider` are wired and the emotion span emits the full acoustic read, but the live
  service needs a GPU; the integration test skips-loud without one.
- **Voice-input STT-confidence as a distinct trace field (MINOR):** the STT span carries transcript +
  engine and the SER span carries the acoustic read; a per-word STT confidence number is not yet its
  own trace field. LLM-call depth (the flagged C1 gap) is complete.
- **Locale capture by inference (PARTIAL):** locale is captured via the profile UI + persisted +
  drives answers; consent-gated automatic inference of timezone/units (vs. explicit entry) is a
  smaller follow-up.

---

## 4. Missing vs. the design doc (checklist)

- [ ] **Design doc §20** — referenced by the combined brief as "new §20" but **absent** from
  `docs/ai-companion-design-doc.md`; nothing to build against until it's written.
- [ ] **Full multi-user trait toggles UI (§12.3)** — explicitly a backlog item in the design doc.
- [ ] **Backlog items intentionally excluded** (per BUILD_STATUS): presence/proactivity beyond
  consent-gated insight, custom wake word, at-rest encryption, external MCP tools, real auth beyond
  the Google SSO stub. These are design-doc "later/backlog", not regressions.
- [ ] **Pitch-preserving voice time-stretch (C7 fidelity):** current speed uses `playbackRate` (mild
  pitch rise at higher rates); a WSOLA server-side stretch is the higher-fidelity follow-up.
- [ ] **C8 visual redesign sign-off** (see §3).

Everything else in design doc §0–§19 + the A1.5 Addendum (swappable engines behind ports:
Whisper/Pipecat/LangGraph/Langfuse/Graphiti/Mem0/Qdrant) is implemented and wired.

---

## 5. Foundational gaps — called out explicitly

- **Does the app genuinely KNOW the user and USE it to shape responses?** **YES, now.** Before this
  round the user model had psychological signals (§17) but NO geolocation/timezone/units/currency/
  language — so answers defaulted to US units and UTC-ish time. Now `LocaleProfile` is captured,
  persisted, injected into the prompt (the identity block shows "FOR THE USER it is currently 15:51 in
  Asia/Kathmandu" + the humanize directive), and the trace records `user_context_signals`
  (`['location','timezone','units','currency','language']`). *Trace evidence the user-model drove the
  answer:* the same "weather in Tokyo" returned Celsius/km for the Nepal user and °F/miles for the New
  York user; the same "time in Spain" was framed "~4.5 hours behind you" for the Nepal user.
- **Real context/intent correlation?** **YES** — internal-vs-live routing, follow-up continuity,
  reference resolution, indirect intent; LLM-judge 1.0 (§2, C3).
- **Deep traces?** **YES** — from one turn's trace you can answer how many LLM calls, each purpose,
  full params, why each, parallel vs sequential, the exact prompt, and why it responded; judge 7/7.
- **Real interruption?** **Mechanism YES** — playback stop + queue flush + drop-trailing + generation
  cancel, demonstrably firing (audio truncated in a real run). The *felt* sub-300ms stop over a real
  mic still wants a human device check.
- **Human-quality answering?** **YES** — humanize directive drives every reply; judge 1.0 (§2, C4).

---

## 6. Honest summary

**Roughly ~88% of the design doc (§0–§19 + Addendum) is truly implemented-and-working**, verified by
real runs this round — the hard, judgment-heavy core (ReAct + self-reflection, memory read/write,
intent/context correlation, human-quality humanized answers anchored to a real user-model,
search-when-uncertain, deep traces, real interruption, Langfuse HITL) all pass real-call + LLM-judged
evaluation. The remaining ~12% is: (a) subjective UI visual-polish sign-off (C8), which is
code-improved and build-green but needs an authenticated browser to redesign/QA visually; (b) a few
config-gated or hardware-gated pieces (continuous automated judge scoring, SER on GPU, felt barge-in
latency on a real mic); and (c) explicit design-doc backlog items.

**The single biggest gap** between the current app and the designed app is now **subjective UI/UX
visual quality (C8)** — the reasoning, memory, voice-control, personalization, and observability core
is genuinely working and judged good, but the *look-and-feel* redesign is the one deliverable I could
not complete or verify unattended because the authenticated pages are behind Google SSO. Granting a
test session (or doing the visual pass in-browser) closes it. The previously-cited foundational gap —
"does it actually know and use the user?" — is **closed**: the app now captures the user's locale and
demonstrably frames answers for them, visible in the trace.
