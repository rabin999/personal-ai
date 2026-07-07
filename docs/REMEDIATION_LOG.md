# Remediation Log

Autonomous hardening pass. Root causes, fixes, and framework adopt/reject decisions.
Newest entries at the bottom of each section. Companion doc: `GAP_ANALYSIS.md`.

---

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
