# Build Status

**Purpose:** running progress tracker so any new session knows exactly where the
build is without re-explaining. Read this first each session; update it when a
module's acceptance criteria pass.

**Legend:** ⬜ not started · 🟨 in progress · ✅ done · ⏸️ blocked

**Done (✅) means:** unit + integration + e2e tests pass, all acceptance criteria pass,
isolation + cost-logging + ports-boundary checks pass, and `uv run ruff check && mypy .
&& lint-imports && pytest` is green. (See CLAUDE.md §6.) Track test levels per module
with the U/I/E markers in the Tests column (e.g. `U✅ I✅ E🟨`).

**Last updated:** 2026-07-09
**Current module:** _(All 26 modules ✅ + assembly ✅ + demo UI ✅ + F1–F21 ✅ + companion-depth U0–U12 ✅ + latency/params/UX pass ✅)_

> **Latency + UX pass (2026-07-09):** shipped the three follow-ups (prompt caching #17,
> Grok STT adapter #18, 26-voice roster + natural default #19), the interactive
> knowledge-graph, and a full latency program: profiled the turn (L0), right-sized the
> reply model + gated context_intent on simple turns (L3/L5), parallelized the context
> reads (L1), disabled fast-model "thinking" (P4), tuned per-step temperature/max_tokens
> (P1–P3) — **simple turns 7032ms → ~1500ms, quality held (judge 4–5/5)**. Switched STT
> default to Grok STT (~10s CPU-whisper → ~1.5s). Fixes from live use: never-speak-first,
> shorter/less-warm/casual voice, name-not-"AI", instant search ack, searchable
> full-catalog model picker (gemini-2.5-flash default), voice↔turn-taking sync, tool-
> syntax leak scrub, tool-call trace args+results, search freshness (recency bias +
> context-rich queries + graceful failure), welcome greeting, mobile fixes. Benchmark
> across 5 fast LLMs → `docs/LATENCY_BENCHMARK.md`. All committed, pushed, deployed;
> non-paid suite green except 3 known env failures (real-Graphiti, SMTP, pipecat audioop).

> **Companion-depth pass (2026-07-08):** **U0–U12 ✅** — see below + docs/TEST_REPORT.md.

> **Companion-depth pass (2026-07-08):** **U0–U2 ✅** — fixed the memory layering so
> facts (semantic), events (episodic), and PERSONA (style) stay in their correct
> sections instead of storing gibberish. New dynamic per-user persona
> (`core/psych/persona.py`) — the "How I've learned to talk with you" layer — that
> evolves (stated→immediate, inferred→accrues, contradiction→supersede) and is
> injected into Prompt Assembly so the same question gets a different STYLE per user.
> Extraction hardened with a quality bar + `style_signals` routing; cleanup pass
> (`core/memory/cleanup.py` + `scripts/clean_memory.py`) removes accreted junk;
> `GraphStore.list_facts`/`delete_fact` added (Neo4j, group-scoped, also feed the
> U4 graph view). Proven by real-call routing + per-user shaping tests
> (`tests/real_call/test_persona.py`) + 7 unit tests. See `docs/TEST_REPORT.md`.
> Remaining U3–U12 (projects view, graph view, user-local time, context ladder,
> cross-turn correlation, dynamic prosody, background delivery, audio awareness) in
> progress.

> **Prod fix pass (2026-07-08):** reported prod issues, root-caused against the live
> stack. **F17 Pipecat prod voice:** `CompanionSTTService` extended the base `STTService`,
> which runs `run_stt` per ~20 ms audio frame — faster-whisper transcribed fragments, so the
> companion never got a real utterance and appeared dead. Fixed to `SegmentedSTTService`
> (VAD-bounded, one `run_stt` per utterance, raw-PCM `wants_wav_segments=False`) + added the
> `UserTurnProcessor` (Pipecat 1.5's turn model) so barge-in interruptions actually fire, and
> dropped the no-op `PipelineParams(allow_interruptions=…)` (removed field in 1.5). Regression
> test drives a real pipeline via `run_test`. **F18 Langfuse prompts visible:** the `llm.call`
> trace record now carries the real messages (incl. the assembled system prompt) + reply →
> promoted to the generation's `input`/`output` (were empty). **F19 Langfuse user tracking:**
> the sink stamps `user_id`/`session_id` on the trace via `propagate_attributes` (per-user
> filtering/cost now works — §3 invariant 1). **F20 trace deep-link:** built from the SDK's
> `get_trace_url` (real project *id* + browser host, cached) instead of the `LANGFUSE_PROJECT`
> *name* that resolved to the wrong place. **F21 live evaluator:** companion-voice LLM-as-judge
> (shared `core/eval/judge.py`) runs per turn OFF the reply path and posts `companion_voice` +
> `chatbot_like` scores to the turn's Langfuse trace, gated by `langfuse_eval_enabled` (default
> off — extra judge call per turn). Unit-tested; FULL non-real-call suite green.

> **Follow-up fix pass (2026-07-08):** completed F1–F16 (voice barge-in AEC-attenuation fix +
> Pipecat prod startup; dual-model Whisper STT; conversation-context + past-conversation recall
> routing; intent inference for indirect asks; operative + trace-visible traits; full verbatim
> per-turn trace incl. self-reflection draft→revision; UI thinking-model selector + external tool
> links + unified app shell + conversation previews + mobile pass; Langfuse prompt management +
> human-eval scoring behind ports; long-session rolling-summary compaction; disabled-mailer
> visibility). Each proven by real E2E + judged where behavioral, logged in `docs/TEST_REPORT.md`.
> Hardware/credential blockers documented honestly: real-mic barge-in audio, on-device mobile
> feel, and Gmail SMTP credentials (welcome email) need a human/device.

> **Autonomous hardening pass (2026-07-07):** root-cause fixes for reported voice/tool/
> memory/style issues, driven by `docs/GAP_ANALYSIS.md`; decisions + full DoD status in
> `docs/REMEDIATION_LOG.md`. Highlights: pre-roll buffer (first words no longer clipped),
> background-delivery de-dup, `record_trade` (trades persist from a cold account), durable
> trace store + `/debug/traces`, forbidden-assistant-speak detector + self-reflection
> rewrite (live-verified the reply stops sounding like a service desk), tool-result store +
> `recall_tool_result`, user-selectable fast model, durable raw conversation store +
> `/api/conversations`, voice sample preview. New backend surfaces: `core/observability`,
> `core/tools/results.py`, `core/memory/conversation_store.py`, `core/reasoning/style.py`,
> and `/api/{models,voices,conversations}` + `/debug/traces`. FULL CHECK green
> (unit + integration). Hardware/tuning items (live barge-in, TTS tag audibility, SER GPU)
> and prompt caching are documented as blocked/deferred with rationale — not silently
> skipped.

---

## Phase 0 — Scaffold & Tooling
_(Setup items — verified by "does it run / does CI pass", not unit tests.)_

| Status | Item | Ref | Notes |
|---|---|---|---|
| ✅ | `uv` project init + `pyproject.toml` (deps + ruff/mypy/pytest config) | spec §0.3 | uv + lockfile; voice stack (Pipecat/Silero) declared as optional `voice` extra, installed in Phase 6 |
| ✅ | Pre-commit hooks + CI (ruff, mypy, lint-imports, pytest) | CLAUDE §4a | Local pre-commit hooks + `.github/workflows/ci.yml`, both run the FULL CHECK |
| ✅ | docker-compose for local deps (Mongo/Qdrant/Neo4j/Redis) | — | `docker compose up -d`; defaults in `config/settings.py` match it |
| ✅ | Project scaffold (dirs, empty `ports/`) | design §17.3 | All 10 ports stubbed; import-linter enforces core ↛ adapters; ports pure (no core/adapters imports) |
| ✅ | FastAPI serving edge skeleton (token→user_id wiring point) | spec §0.6, §26 | `api/deps.get_user_record` resolves bearer → UserRecord via port; 501 until §26 adapter wired; smoke-tested |

## Phase 1 — Foundation
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ✅ | §1 Database Layer | spec §1 | — | U✅ I✅ E✅ | `adapters/db.py`; pooled clients, fail-loud startup, Qdrant collections (dense+sparse, user_id index); Graphiti lazily wired to OpenRouter (models finalized in §6) |
| ✅ | §26 User Context (static auth) | spec §26 | §1, §2 | U✅ I✅ E✅ | Static map in `config/defaults/static_users.json` (2 users); resolve → first-run sync; e2e over HTTP via CurrentUser dep; app lifespan wires adapters |
| ✅ | §2 Config & User Profile | spec §2 | §1 | U✅ I✅ E✅ | Built before §26 (its dependency). `traits_enabled` stores overrides only (?? default per rule 3). Seed trait descriptions are DRAFT — human tuning pending (§7 hand-off) |
| ✅ | §3 Cost Ledger | spec §3 | §1 | U✅ I✅ E✅ | `core/cost`; fire-and-forget log (p95 latency test), $group aggregation + breakdown, project_spend w/ ISO range, user-scoped queries, failed writes swallowed |

## Phase 2 — Memory
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ✅ | §4 Working Memory | spec §4 | — | U✅ I– E✅ | `core/memory/working.py`; pure in-memory so integration n/a; e2e via §5 memory-flow test |
| ✅ | §5 Episodic Memory | spec §5 | §1 | U✅ I✅ E✅ | fastembed local embedder (bge-small, 384d) + Qdrant/bm25 sparse; RRF via query_points; turn-based chunking; gentle recency half-life; isolation verified |
| ✅ | §6 Semantic Memory | spec §6 | §1 | U✅ I✅ E✅ | Graphiti group_id=user_id; gemini-2.5-flash extraction (gpt-4.1-mini dropped edges), temp 0, json_object mode; fastembed embedder; LLM usage → Cost Ledger via httpx hook; paid tests skip loudly without OPEN_ROUTER_API_KEY |
| ✅ | §7 Procedural Memory | spec §7 | §1 | U✅ I✅ E✅ | Confidence 0.3 start / 0.6 injection threshold / ±delta clamp [0,1]; context filter by trigger words; e2e arrives with §10/§18 (integration covers today's full path — stated per contract §6) |
| ✅ | §8 Entity Resolution | spec §8 | §1, §5 | U✅ I✅ E— | Deterministic point ids (rename = in-place update); is_ambiguous helper (runner-up ≥0.8×top) for §12's disambiguation; e2e arrives with §10 step 2 (integration covers today's full path) |

## Phase 3 — Reasoning Core (text-only first)
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ✅ | §9 Self-Model (metacognition) | spec §9 | §1, §11 | U✅ I✅ E✅ | `self_statements` Qdrant collection (§9's namespace); heuristic overclaim patterns + judgment flag → LLM rewrite (retry once → safe fallback); patterns/wording flagged for human tuning; per-turn-log e2e green via §12 turn |
| ✅ | §10 Prompt Assembly | spec §10 | §2,4,5,6,7,8,9,16 | U✅ I✅ E✅ | Ordered pipeline + char-budget trimming (episodic→facts→self→project→rules; utterance/WM/entities untouchable); §16 injects via ProjectContextProvider protocol (stubbed); heuristic complexity hint; e2e green via §12 turn |
| ✅ | §11 LLM Router (OpenRouter) | spec §11 | §3, §2 | U✅ I✅ E✅ | Built before §9/§10 (their dependency). Tier chains in provider_config `llm_router`; exact cost via OpenRouter usage accounting; embed() is local fastembed; e2e via §12 turn |
| ✅ | §12 Response Gen + behavior gates | spec §12 | §10,§11,§9 | U✅ I✅ E✅ | ⚠️ mechanism complete, thresholds/wording await human tuning: curiosity gate (trait params), overclaim rewrite, pull-based disclosure, Pydantic retry→safe fallback; full-turn e2e green (memory recall via real LLM) |

## Phase 4 — Tools & Projects
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ✅ | §13 Tool Dispatcher | spec §13 | §14,§3,§11 | U✅ I✅ E✅ | MCP-shaped registry (id/desc/schema/handler); shielded action writes (barge-in safe); 800ms variable budget → queue promotion; ReAct loop w/ validated JSON steps; e2e green via project-flow test |
| ✅ | §14 Background Task Queue | spec §14 | Redis | U✅ I✅ E✅ | Built before §13 (its dependency). Redis lists+JSON records; TaskWorker handler registry; DeliveryComposer: LLM judges relevance → interject or suppress (never templated) |
| ✅ | §15 Web Search | spec §15 | §11,§14,§3 | U✅ I✅ E✅ | Cache in Mongo w/ per-query-type TTL (15m time-sensitive / 24h stable); Serper live-verified, Brave adapter key-gated; summarize via simple tier; runs as §14 task (e2e = §14 delivery flow) |
| ✅ | §16 Projects | spec §16 | §1,§8,§3,§13 | U✅ I✅ E✅ | finance_portfolio blueprint seeded; avg-cost P&L from append-only ledger; type actions registered only when an instance exists; consent-gated insight w/ caveat; implements §10 ProjectContextProvider |

## Phase 5 — Learning
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ✅ | §17 Psychological User-Model | spec §17 | §1 | U✅ I✅ E✅ | ⚠️ mechanism complete, inference quality human-validated: confidence-gated OCEAN nudges, rolling mood baseline + deviation detect, stage-of-change; prompt rendering tested against clinical language |
| ✅ | §18 Learning & Adaptation | spec §18 | §6,§7,§17,§9,§14 | U✅ I✅ E✅ | ⚠️ mechanism complete, human validates learning: LLM session analysis → rule reinforce/contradict/add, mood roll-in, correlation candidates gated at 3 sightings; runs as queued §14 task (live path <0.5s) |

## Phase 6 — Voice
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ✅ | §19 Audio Input Pipeline | spec §19 | Pipecat/LiveKit | U✅ I✅ E✅ | Toggleable stages; clamped VAD; VADGate cost-gate (idle-is-free); raw per-frame `is_speech` (endpoint timing) vs hysteretic `speech_active` (paid gate); AEC↔barge-in warn; ambient window. Real VAD = `adapters/vad/silero.py` (pipecat's own voice_confidence mis-indexes the 2-D output + wrong frame size → we call the model directly, 512-sample frames). I = real Silero, speech-detection asserted |
| ✅ | §20 STT Adapter | spec §20 | §6,§3 | U✅ I✅ E✅ | faster-whisper local ($0, ledger-logged in seconds; OpenRouter has no transcription endpoint — Grok is TTS-only per spec §32 table, so STT stays Whisper); windowed partials → final w/ per-word confidence; vocab-boost via initial_prompt now **seeded live** from Semantic Memory (`core/memory/vocab.py` → VoiceSession) — the user's names/terms (NEPSE/Trishul/companion name). I = real tiny model (skip-loud offline) |
| ✅ | §21 Semantic Endpointing | spec §21 | §20,§2 | U✅ I– E✅ | Silence + lexical completeness (filler/trailing-conjunction aware), rising-prosody defer; per-user thresholds. Pure logic → integration n/a (stated per §6, like §4) |
| ✅ | §22 SER Service | spec §22 | emotion2vec | U✅ I✅ E✅ | ⚠️ inference quality human-validated. emotion2vec GPU microservice (`services/ser_service`, `ser` extra) + thin httpx client (Pydantic-validated, retry→neutral fallback); label→valence/arousal map; LaggingEmotionProvider runs one turn behind; self-hosted $0 (unlogged). Feeds §10+§17. I = real service (skip-loud, needs GPU) |
| ✅ | §23 TTS Adapter | spec §23 | §11,§12,§3 | U✅ I✅ E✅ | **Grok Voice TTS** via xAI `POST /v1/tts` (the spec's chosen voice; endpoint is /v1/tts, NOT OpenAI-style /audio/speech). 5 voices (ara/eve/leo/rex/sal), inline delivery tags, PCM16 streamed, ~$4.20/1M chars logged; clause chunking never splits a tag; interruptible. Key = `X-AI-API`. I = real endpoint live-verified |
| ✅ | §24 Barge-in & Interruption | spec §24 | §19,§23,§11,§13 | U✅ I– E✅ | Stops TTS + cancels generation on speech; action-write protection defers interrupt until write commits (rule 3); AEC dependency validated in §19. Pure asyncio → integration n/a; E covers §19 VAD-event→interrupt+write path |

## Application Assembly (post-module: wiring the 26 modules into a running app)
_(The 26 modules were built and unit/integration/e2e-tested in isolation; this
layer assembles them into a runnable product — serving edge, live voice
runtime, background worker, demo UI.)_

| Status | Piece | Ref | Tests | Notes |
|---|---|---|---|---|
| ✅ | Composition root | design §17.2 | E✅ (live boot) | `api/composition.py` — single place adapters are wired to core via ports; loads tier chains + pricing from seeded `provider_config` (config over code); boots green against live datastores |
| ✅ | Voice session runtime | design §17.1 | U✅ E✅ | `voice/session.py` + `voice/trace.py`; **continuous** turn-taking (not push-to-talk): §19 gate → §20 STT → §21 endpoint auto-detect utterance boundaries → §10 → §12 → §23, with §24 barge-in (START_FRAMES fresh-speech hysteresis, no self-interrupt) + one-turn-behind §22. TraceEvents grouped by turn; idle short-circuits before any paid stage. Live e2e verified end-to-end (real VAD→STT→LLM→Grok TTS) |
| ✅ | Serving edge (API) | spec §0.6 | U✅ (routes) E✅ | `api/routes/voice.py` WS (auth-in-first-msg §26, PCM frames → trace JSON + TTS audio, barge-in), `api/routes/chat.py` text turn; `api/streaming.py` merge/reframe; verified a real text turn over HTTP end-to-end |
| ✅ | Background worker | spec §3, §14 | U✅ | `workers/consolidation_worker.py` deployable entrypoint; registers `tool`/`web_search`/`consolidation` handlers on the §14 queue via the composition root |
| ✅ | §17 → §10 wiring | §17 rule 3 | U✅ | Reconciled spec inconsistency: §17 says its soft signals feed §10, but §10's step list omitted it. Added optional psych provider to Prompt Assembly (empty until confident; wording via `describe_for_prompt`, tuning yours §7) |
| ✅ | Demo UI | (new, user-requested) | build✅ | `web/` Vite+React 19+TS+Tailwind v4: mic picker, amplitude-reactive talking orb, single **Start/Stop conversation** toggle (continuous — no push-to-talk), **collapsible per-turn trace** cards (newest open) with **replayable reply audio** per turn, WS audio (AudioWorklet PCM16@16k up, 24k Grok playback, barge-in). `tsc -b && vite build` green; FastAPI serves `web/dist` |

**Removed:** dead `adapters/stt/openrouter_whisper.py` (OpenRouter exposes no
transcription endpoint; faster-whisper local is the STT — §20).

---

## Cross-cutting checks (verify periodically, not one-time)
- [ ] No `core/` file imports from `adapters/` (grep / lint-imports to confirm).
- [ ] Every store query includes a `user_id` filter (isolation).
- [ ] Two-user isolation test passes (§26 acceptance): user A never sees user B's data.
- [ ] Every paid-provider call has a corresponding Cost Ledger entry.
- [ ] No backlog items were built (presence, custom wake word, encryption, external MCP, real auth).

---

## Notes on history
This file tracks **current state only** (what's done, right now). The record of *what
happened each session* is the **git commit history** — each module is committed with a
message referencing its spec section. There is no separate session-log file to keep in
sync. Update this file when a module's status changes; let git carry the history.