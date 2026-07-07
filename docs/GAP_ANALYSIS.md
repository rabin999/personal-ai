# Gap Analysis — spec vs. actual code

**Author:** autonomous remediation pass · **Date:** 2026-07-07
**Method:** read `CLAUDE.md`, design doc, MVP build spec (26 modules), `BUILD_STATUS.md`,
then the actual codebase module-by-module. Each module classified
**compliant / partial / missing / wrong** with the concrete deviation.

> **Headline:** the app is *far* more built than the remediation brief assumes — 26
> modules exist, are wired through a real composition root, and unit tests are green.
> It has **not** "drifted into a generic assistant" at the config level: the full tone
> standard already lives in `config/defaults/trait_defs.json` (`response_voice`,
> `emotional_intelligence`) and is composed into the system prompt by §10. The real
> defects are (a) a handful of concrete **root-cause bugs** in the voice runtime, tool
> delivery, and projects surface, and (b) **missing enforcement/observability** — no
> durable trace store, no forbidden-phrasing regression test, no user-selectable model,
> no way to *create* a project from voice. Those are what this pass targets.

Legend: ✅ compliant · 🟨 partial · ❌ missing · ⚠️ wrong/bug

---

## Per-module audit

| Module | Status | Deviation / evidence |
|---|---|---|
| §1 Database | ✅ | `adapters/db.py`, pooled clients, Qdrant collections. Fine. |
| §2 Config & Profile | 🟨 | Trait registry + seeded config good. **No per-user model selection field** (§4 asks for it). Endpointing/VAD thresholds are in `AudioPrefs` (good). |
| §3 Cost Ledger | ✅ | `core/cost`, fire-and-forget, user-scoped. Fine. |
| §4 Working Memory | ✅ | In-session recent turns present and used by §10. |
| §5 Episodic | 🟨 | `EpisodicMemory` writes per-turn (`voice/session._remember`) and retrieves in §10. **No dedicated durable raw-conversation store** separate from derived memory (brief §6 asks for one). Recall reliability not live-verified. |
| §6 Semantic | ✅ | Graphiti group_id=user_id extraction. Fine at the interface level. |
| §7 Procedural | ✅ | Confidence-gated rules, context filter. Fine. |
| §8 Entity Resolution | ✅ | Deterministic ids, `is_ambiguous`. Fine. |
| §9 Self-Model | 🟨 | `check_boundary` overclaim rewrite exists. **No full Reflexion-style self-critique pass** (§9.3 of brief) beyond overclaim. |
| §10 Prompt Assembly | ✅ | Ordered pipeline, trait composition, char-budget trim. Traits *do* reach the prompt. |
| §11 LLM Router | 🟨 | Tier chains from `provider_config`. **Fast tier is fixed config, not user-selectable** (§4). |
| §12 Response Gen | 🟨 | Judgment block + gates + agentic tool loop present and correct. **No forbidden-phrasing enforcement test** — style relies entirely on the model honoring the trait. |
| §13 Tool Dispatcher | ⚠️ | Correct class-based dispatch + ReAct loop. **BUG: two tool loops exist** — `ResponseGenerator.generate` (the live one) *and* dead `ToolDispatcher.loop`. **Tool results are not persisted** anywhere (§5.2 of brief). |
| §14 Background Queue | ⚠️ | Redis queue + `DeliveryComposer` present. **BUG: deferred delivery can double-fire** — `_deliver_pending` is called from *both* the idle poll and the start of every turn with no mutual exclusion → the "top N shows the same item 2–3×" class of duplication. |
| §15 Web Search | ✅ | Cache-first, Serper→Brave fallback, summarized, cost-logged. Fine. |
| §16 Projects | ⚠️ | Ledger P&L, consent-gated insight all present. **BUG/GAP: `create()` is never exposed as a tool or called from voice** → "record my trade/share" cannot create a `finance_portfolio` instance, so `log_entry` never registers → nothing persists. Root cause of the memory-of-trades complaint. |
| §17 Psych Model | ✅ (mech.) | Confidence-gated OCEAN, mood baseline, stage-of-change. Human-tuning module. |
| §18 Learning/Consolidation | ✅ (mech.) | Session analysis → rule updates, runs as queued task. Human-tuning module. |
| §19 Audio Input | ⚠️ | VAD gate / idle-is-free correct. **BUG: no pre-roll buffer** — `voice/session._consume` resets `buffer=[]` at `speech_start`, which only fires after `START_FRAMES=3` frames, so ~100ms of onset is dropped. Root cause of "first words are cut off." |
| §20 STT | ✅ | faster-whisper local, live vocab boost from semantic memory. Fine. |
| §21 Endpointing | 🟨 | Silence + lexical completeness, config-driven thresholds. Default `short_pause_ms=700` is slack (brief §2.3 wants it ~100ms tighter). |
| §22 SER | ✅ (mech.) | emotion2vec microservice + lagging provider, feeds §10/§17. Needs GPU to live-verify. |
| §23 TTS | 🟨 | Grok `/v1/tts`, 5 voices, clause chunking that never splits a tag, tag sanitizer. **No voice-sample-preview surface** (brief §3.2). Tag audibility not live-verified (needs a key + ears). |
| §24 Barge-in | 🟨 | Cancels TTS + generation on fresh speech; action-writes shielded. Correct in code. **Live immediacy depends on AEC** (transport-provided) and on the client streaming mic frames *during* playback — not verifiable without hardware. |
| §26 User Context | ✅ | Static token→user, first-run sync. Fine. |

---

## Cross-cutting gaps (the remediation brief's themes)

1. **Observability (§1 of brief).** Trace events exist (`voice/trace.py`) and stream to the
   UI, but are **ephemeral** — not persisted, not queryable after the fact, no debug
   endpoint. *Decision (see REMEDIATION_LOG): persist traces to Mongo + add a debug
   endpoint rather than stand up full Langfuse — meets durable/queryable/inspectable with
   zero new infra; Langfuse recommended as a later enhancement.*
2. **Duplication (§5.1/§5.4).** Root cause is the un-guarded double `_deliver_pending`, not
   the ReAct loop. Fixed with a delivery lock + delivered-id guard.
3. **First-word capture (§2.2).** Root cause: no pre-roll ring buffer. Fixed.
4. **Model selection (§4).** No per-user selectable fast model. Added `model_prefs` to the
   profile + router honoring it + latest fast default.
5. **Tool-result persistence (§5.2).** No `tool_results` store. Added, wired into dispatch,
   plus a `recall_tool_result` tool so "what was that news?" resolves.
6. **Record-my-trade (§6/§16).** No conversational path to create a project instance. Added
   a `record_trade` action tool that creates the instance on first use then logs the entry.
7. **Response-style enforcement (§7).** No test fails on forbidden phrasings. Added a
   regression golden test scanning generated text for assistant-speak.
8. **Self-reflection (§9.3).** Only overclaim-checking. A fuller quality/duplication/format
   self-critique pass is a recommended follow-up (mechanism sketched in the log).

## Items requiring hardware / live services / human tuning (documented, not faked)
- Live barge-in immediacy & AEC (needs mic + duplex audio client).
- Grok TTS tag *audibility* (needs the key live + listening).
- SER inference quality (needs the GPU microservice running).
- Response-tone *feel* and gate thresholds (§7 hand-off — human-tuned by design).
- Full Langfuse/Pipecat/LiveKit adoption (large migrations; see REMEDIATION_LOG rationale).
