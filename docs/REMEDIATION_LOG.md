# Remediation Log

Autonomous hardening pass. Root causes, fixes, and framework adopt/reject decisions.
Newest entries at the bottom of each section. Companion doc: `GAP_ANALYSIS.md`.

---

## Second pass — memory triggers, extraction loop, faster CI (2026-07-07)

### Doc overrides (made to make the app actually work; per instruction)
- **Memory has a single explicit WRITE step.** Implemented the read → reason → WRITE loop:
  after responding, an LLM-driven, Pydantic-validated **extraction step**
  (`core/memory/extraction.py`) decides what to persist and WHERE — episodic *events*,
  distilled *semantic facts* (time-stripped, e.g. "takes BP meds daily ~8pm"), and *trades*
  — instead of dumping every turn verbatim (the raw `ConversationStore` still keeps the full
  log). This overrides the previous "write the whole turn to episodic" behavior.
- **Removed the `record_trade` conversational tool** (added earlier this session as R3).
  Reason: it created a SECOND trade-write path, so a trade got logged twice (once by the
  chat model calling the tool, once by extraction → net_invested doubled in the e2e). Trades
  now persist ONLY through the extraction step — one writer, no double-write. `record_trade`
  the tool is gone; `ProjectService.find_or_create` + `log_entry` remain (extraction uses them).
- **Pre-commit is now fast (ruff only).** Heavy checks (mypy, full pytest, lint-imports) run
  ONCE per bundle via `./scripts/check.sh`, not on every commit — per the instruction to stop
  burning budget on per-commit suite runs.

### Mechanisms added this pass
- **Duplicate-action guard** in the agentic loop: an `action` tool runs at most once per turn
  (dedup by id — the model jittered args when logging the same trade 3-4×). Read/background
  tools still dedup by id+args. Fixes the "top N / same write repeated" class at the loop level.
- **Deterministic style scrub** (`style.scrub_forbidden`): if the self-reflection rewrite is
  still assistant-speak, drop the offending sentence so a banned shape can never ship (keeps
  the rest; never empties the reply). Belt-and-suspenders behind the §9.3 rewrite.
- **Core-engine e2e proof** (`tests/acceptance/test_core_engine_e2e.py`, paid+integration):
  drives the real pipeline from the text boundary — trade record→recall, the medication
  routine (extraction distills a semantic fact → recalled in a NEW session), cross-session
  fact recall, no assistant-speak across a conversation, and cross-user isolation. All pass
  against live datastores + real model.

## Third pass — Mem0 wired + memory-correctness bugs from the audit (2026-07-07)

### R11 — Mem0 preference memory ADOPTED + wired (brief §2)
Installed `mem0ai` and wired it as the fast personalization/preference layer, configured to
OUR stack (no OpenAI key): LLM via OpenRouter, embedder via local `fastembed` (same bge-small
model), vector store in our Qdrant (`mem0_preferences` collection). Port
`ports/preference_memory.py`, adapter `adapters/preference/mem0_adapter.py` (guarded init +
best-effort calls, blocking client run in a thread). Wired: WRITE in the extraction step
(`MemoryExtractor` calls `preferences.add`), READ in prompt assembly (a "What you know about
this person" section, `preferences.search`). Config-gated by `preference_memory_enabled`.
Complements Graphiti (temporal facts) — see the two-engine note below.

### R12 — Extraction re-stored recalled info (double-write) — FIXED
**Root cause (found by the audit conversation):** when the user asked *"what did I buy?"* and
the companion restated *"you bought 10 SYPNL"*, the extractor treated the restatement as a new
statement and logged the trade AGAIN (ledger doubled to 2 entries / 4600).
**Fix:** (1) strengthened the extraction prompt to store ONLY genuinely NEW user statements —
questions and companion recall/confirmation → `store_nothing`; (2) a deterministic
duplicate-trade guard in `_store` that skips a trade already in the ledger. Verified live: the
recall turn now `WROTE -> nothing` and the ledger stays at `entry_count: 1 / 2300`. Tests:
`test_extraction.py::test_same_trade_is_not_relogged_on_recall`.

### Note on Graphiti semantic retrieval
The audit run showed `profile_facts` returning `(none)`; the follow-up run (after wiring)
returned the fact correctly (`'user takes blood-pressure prescription daily at 8pm'`). It
appears timing/indexing-sensitive rather than fully broken; Mem0 now also backstops
preference/fact recall. A residual Graphiti warning (`Source entity not found in nodes for
edge relation`) remains and is logged for follow-up. Recall is currently robust via episodic +
Mem0 even when Graphiti lags.

## Fifth pass — tracing completeness + Graphiti retrieval root-cause fix

### R16 — Trace now has per-LLM-call spans + judgment + reflection (CLAUDE.md §5)
Every OpenRouter call emits an `llm.call` span (model, in/out tokens, cost_usd,
latency_ms, tier); `ResponseGenerator` emits a `judgment` span and a distinct
`reflection` span. A `TraceStoreLogSink` maps correlation-bound structured-log records
into the durable trace store, so all spans show up in `/debug/traces` + the /traces UI,
grouped by session. The chat route binds `trace_id/turn_id/user_id` around the whole turn.
Verified live: `llm → judgment → llm → reflection`, reflection catching a "flat filler
opener" and cleaning it. This is the "hand-rolled trace equally complete" bar.

### R17 — Graphiti retrieval returned nothing (the audit's #1 blocker) — ROOT-CAUSE FIXED
**Root cause (proven live):** a bare fact episode ("takes meds at 8pm") makes Graphiti's
extractor **orphan the edge** ("Source entity not found in nodes for edge relation") so
`search` returns nothing; the SAME fact with a subject ("The user takes…") returns
reliably. Direct A/B: bare → `[]`, subjected → `['user takes … 8pm']`.
**Fix:** the extraction step now gives every semantic fact an explicit user subject before
writing to Graphiti (`_with_subject`). Verified end-to-end: `profile_facts` returns the
fact and a NEW session recalls "8 PM". Semantic/temporal retrieval is now reliable, not
just backstopped by Mem0/episodic.

## Fourth pass — logging transport + per-user UI (Parts B & C)

### R13 — Pluggable logging transport (Part B)
`LogSink` port + `FileLogSink` (JSON Lines) / `StdoutLogSink` adapters + config factory
(`log_sinks`), `StructuredLogger` (core) fans one JSON record to all active sinks with
per-turn correlation ids (`trace_id`/`turn_id`/`user_id`) bound via a contextmanager. Chat
route emits `turn.request`/`turn.response`. Tested.

### R14 — Per-user pages backends (Part C)
`/api/conversations` (paginated + server-side ISO datetime range, date-fns on the client),
`/api/memories/{semantic,episodic,procedural}` (grouped, paginated, episodic delete),
`/debug/traces`, and `/api/feedback` (thumbs up/down + note tied to session/turn/trace).
Added `VectorStore.list_by_user`/`delete` + `EpisodicMemory.list_recent`/`delete`.

### R15 — Per-user UI (real routes, not hash)
Switched `web/App.tsx` HashRouter → **BrowserRouter** with real named paths `/conversations`,
`/memories`, `/traces` (+ nav). Built the three pages: conversations (paginated, datetime
range), memories (grouped by type, forget-a-memory), traces (readable per-turn view +
thumbs up/down/note feedback). Server SPA fallback is a **404 handler** (not a catch-all
route) so it never shadows API/probe routes or the 401 challenge. `npm run build` green
(tsc + vite). date-fns adopted for date formatting.

## Framework decisions (adopt / reject + why)

| Area | Decision | Rationale |
|---|---|---|
| Observability | **Persist traces to Mongo + debug endpoint** (reject full Langfuse *for now*) | Trace events already exist and stream to the UI; they were only missing durability. A `turn_traces` Mongo collection + `GET /debug/traces/{session}` meets the brief's "durable, queryable, inspectable" bar with **zero new infrastructure**, consistent with the local-first/cost-conscious stack. Standing up a self-hosted Langfuse server + SDK is a larger change with its own ops surface; recommended as a follow-up once the pipeline is stable. The trace schema is span-compatible so a Langfuse exporter can be added later without touching call sites. |
| Voice pipeline | **Keep the current asyncio runtime; fix the real bugs** (defer Pipecat/LiveKit migration) | The reported voice defects trace to *specific, fixable* bugs (no pre-roll buffer; delivery double-fire), not to the runtime being fundamentally wrong. The gate/endpoint/barge-in state machine is sound in code. A full framework migration is high-risk and unverifiable without duplex-audio hardware in this environment. Pipecat adoption remains the right call for production AEC/transport and is recommended, but is out of scope for an unattended pass that cannot A/B it against a mic. |
| Memory | **Keep Graphiti (§6) + Qdrant (§5); add a raw tool-result store** | Graphiti/Qdrant are already wired per spec. The gap was durability of *tool outputs* and a conversational path to *create* project instances — both added without swapping frameworks. Mem0 not adopted: Graphiti already covers the semantic/temporal role the spec assigns; adding Mem0 would duplicate it. |
| Search | **Keep Serper→Brave** | Already correct. No phantom tools present in the registry (no "deep research"). |

---

## Root causes found & fixed

### R1 — First words clipped (§19 / brief §2.2) — FIXED
**Root cause:** the VAD gate fires `speech_start` only after `START_FRAMES=3`
consecutive speech frames, so the onset frames that opened the gate (and the quiet
lead-in) precede the event; `VoiceSession._consume` then reset the capture buffer to
`[]` at `speech_start`, discarding ~100ms of the first word.
**Fix:** a rolling pre-roll ring (~320ms) of pre-speech frames, seeded into the buffer
on `speech_start`. Trace reports `preroll_frames`. Regression test in
`test_voice_session.py::test_preroll_recovers_onset_frames_the_gate_swallowed`.

### R2 — "Top N shows the same item 2-3x" (§14 / brief §5.1, §5.4) — FIXED
**Root cause:** `_deliver_pending` is called from both the idle poll and the start of
every turn with no mutual exclusion; two concurrent pulls could return the same finished
task before either marked it delivered → the same result spoken 2-3×.
**Fix:** per-session `asyncio.Lock` around the pull→mark window + a `delivered_ids` guard
so a result is spoken exactly once. Test
`test_background_result_delivered_at_most_once`.

### R3 — "Record my trade" doesn't persist (§16 / brief §6) — FIXED
**Root cause:** `ProjectService.create()` existed but was never exposed as a tool or
reachable from conversation; the finance `log_entry` action only registers once an
instance exists — so there was no way to create the instance from voice, hence nothing
to write to.
**Fix:** `ProjectService.find_or_create()` + a first-class `record_trade` action tool
(always available) that creates the user's `finance_portfolio` on first use, then logs
the entry. Feeds P&L + prompt context. Tests in `test_projects.py`.

### R4 — Traces were ephemeral (§1 observability) — FIXED
**Root cause:** trace events streamed to the UI but were never persisted → not queryable
after the fact, no inspection surface.
**Fix:** `core/observability/TraceStore` appends every event to a user-scoped
`turn_traces` Mongo collection; `merge_conversation` gained a fire-and-forget `on_event`
sink (never blocks the WS send path); `GET /debug/traces` + `/debug/traces/{session}`
expose a user's own traces (auth'd, isolation-scoped). Tests in `test_trace_store.py`
(ordering + two-user isolation). Also removed a duplicate `queue/dispatcher/delivery/
web_search` construction block in the composition root (dead second instances).

### R5 — No enforcement of the anti-assistant-speak tone standard (§7) — MECHANISM ADDED + confirmed live finding
**Root cause of the symptom:** the tone standard is in config and reaches the prompt, but
nothing *checked* the model honored it, so service-desk phrasings shipped silently.
**Fix (mechanism, per §7 hand-off — no wording decided here):** `core/reasoning/style.py`
detects forbidden assistant-speak / ToS-disclaimer shapes; `GenerationResult.style_flags`
carries them; the voice runtime logs a `generation` **warn** trace when a reply slips.
Gating regression tests: detector coverage + a config-guard asserting `response_voice`
still bans the shapes (`tests/golden/test_gs3_style.py`).
**Confirmed live finding (for the human §7 tuner):** a paid, non-gating diagnostic runs the
*real* fast model with the faithfully-composed traits over tempting openers. **4 of 5 bare
greetings still produced service-desk openers** even with the trait present, e.g. `"hi"` →
*"Hey there! How can I help you today?"*. So the trait wording alone does **not** reliably
suppress the opener — this reproduces the reported complaint. Recommended next lever
(mechanism; final tuning yours): a **self-reflection/rewrite pass** (brief §9.3) that, when
`style_flags` is non-empty, has the model re-say the line in-voice before it leaves.

### R6 — Self-reflection rewrite pass (brief §9.3) — IMPLEMENTED, live-verified
**What:** `ResponseGenerator._rewrite_assistant_speak` — one bounded, tone-neutral rewrite
that fires only when `find_forbidden(text)` is non-empty. Generic instruction (strip the
*shape* of assistant-speak, keep intent), accepts the rewrite only if strictly cleaner,
else keeps the original; provider-down → keep original. Config-gated by a `self_reflect`
constructor flag so the human can disable/tune it (§7).
**Verified:** the paid real-model diagnostic that previously failed **4/5** bare greetings
now passes **5/5** with reflection on — the model's `"How can I help you today?"` opener is
re-said as e.g. a warm greeting. Unit tests cover rewrite-applied, rewrite-rejected-if-not-
cleaner, and reflection-off. This directly fixes the reported #1 issue at the mechanism
level; wording of the rewrite instruction remains human-tunable.

### R7 — Tool results weren't persisted; "what was that news?" couldn't resolve (§5.2) — FIXED
**Root cause:** tool outputs (esp. web_search/news) were used in-turn and discarded — no
store, so a later "what was that news?" had nothing to resolve against.
**Fix:** `core/tools/results.py` `ToolResultStore` persists every result to a user-scoped
`tool_results` collection (keyed by user + ts + tool + query). Wired into the dispatcher's
inline path AND its background `task_handler`, so both sync and queued tool results persist.
Added a `recall_tool_result` core tool so the model can answer questions about a recent
lookup from real stored output. Tests cover store ordering/isolation, dispatcher
persistence, and the recall tool.

### R8 — Fast model not user-selectable; post-speech wait slack (§4, §2.3) — FIXED
**§4:** added `ModelPrefs.fast_model` to the profile, an optional `model` arg to
`LLM.complete` (tried first, tier chain kept as fallback; unknown ids ignored),
`OpenRouterLLM.fast_model_choices()` (simple+moderate, de-duped), assembly sets
`model_override` on non-complex turns only (hard turns still hit the strong tier), and
`GET/PATCH /api/models` so the frontend can list + select. Config-driven end to end.
**§2.3:** tightened `endpoint_short_pause_ms` default 700 → 600 (config-driven, per-user).
Tests: `test_model_selection.py` (choices, non-complex-only override).

### R9 — No durable raw conversation log (§6 "store ALL conversations") — FIXED
**Root cause:** turns were written only to *derived* memory (episodic embeddings), so
there was no verbatim, queryable history — if consolidation/embedding failed or was
tuned, the raw exchange was gone, and there was nothing for a `/conversations` view.
**Fix:** `core/memory/conversation_store.py` `ConversationStore` — append-only raw
`conversation_turns` + a per-session `conversations` header, user-scoped, best-effort
(never blocks a turn). Wired into `VoiceSession` (records each exchange alongside the
episodic write, cross-referenced to the trace turn). `GET /api/conversations` (paginated
+ **server-side** ISO datetime-range filter) and `/api/conversations/{session}` expose a
user's own history. Tests cover recording, pagination, server-side range filter, and
two-user isolation. (Also lays the foundation the follow-up `/conversations` page needs.)

### R10 — No voice sample preview (§3.2) — FIXED
`GET /api/voices` lists the five Grok voices; `GET /api/voices/{voice}/sample` synthesizes
a short line and returns a **playable WAV** (browsers can't play raw PCM16). Auth'd, cost
logged by the TTS adapter. Tests: list, WAV RIFF header, unknown-voice 404.

---

## Definition-of-Done status (honest, at end of this pass)

FULL CHECK green: `ruff` ✅ · `mypy .` (175 files) ✅ · `lint-imports` ✅ ·
`pytest` ✅ (unit + integration against live datastores; paid deselected, offline-only
skipped loudly).

**Done + verified (root-cause):** R1 first-word pre-roll · R2 delivery de-dup · R3
record_trade persistence · R4 durable trace store + `/debug/traces` · R5 style detector
+ config-guard · R6 self-reflection rewrite (live-verified 4/5→5/5) · R7 tool-result store
+ recall · R8 user-selectable fast model + tighter endpointing · R9 durable conversation
store + `/api/conversations` · R10 voice preview.

**Present as mechanism, not live-verifiable here (hardware/services):**
- Barge-in immediacy (§2.1): cancel path is correct in code; true instant halt needs AEC +
  a duplex-audio client — not reproducible without a mic.
- Grok TTS tag *audibility* (§3.1): tags are generated, sanitized, and chunked so a tag is
  never split; whether they *sound* right needs a live key + ears.
- SER prosody (§3.3): `LaggingEmotionProvider` is wired to feed §10/§17 and is traced;
  inference quality needs the emotion2vec GPU service running.

**Deliberately deferred (with rationale, not silently skipped):**
- Prompt caching (§9.1): NOT hard-coded. OpenAI/Google/Anthropic already do automatic
  server-side prefix caching for the stable system prefix, and OpenRouter usage accounting
  already logs $0 on those. Injecting explicit `cache_control` breakpoints risks breaking
  `json_object` mode across the mixed provider set and cannot be safely verified here.
  Recommended as a targeted follow-up on the Anthropic (complex) tier only.
- Full Langfuse / Pipecat-LiveKit adoption: see the framework-decision table above.
- Response-tone final wording (§7): human-tuned by design; the mechanism (detector +
  reflection) is in and the config-guard prevents regressions.
