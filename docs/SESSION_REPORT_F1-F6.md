# Session report — F1–F6: the live voice path was dead

**Date:** 2026-07-09 · **Branch:** `main` · **Pushed:** `c3dfd1d..0951669` (8 commits)
**Scope:** repair + verify the live voice path. **No latency optimization was done** (the original
optimization brief was cancelled once the outage was found — every number in
`docs/LATENCY_ANALYSIS.md` describes a function the app could not reach).

This file is the entry point. The detail lives in:

| Document | What's in it |
|---|---|
| `docs/CODE_FLOW.md` | Map of one voice turn, file → function → what it calls next. §0 documents the two-entrypoint split. |
| `docs/LATENCY_BASELINE_REAL.md` | The true latency baseline, measured through `VoiceSession`. Supersedes `LATENCY_ANALYSIS.md`. |
| `docs/NEXT_CORRECTNESS_TASK.md` | Two diagnosed-but-unfixed defects, with evidence. **Read item 0 first.** |
| `docs/TEST_REPORT.md` §F1–F6 (line ~2052) | Full write-up incl. the audit of claims that are now unverified. |
| `docs/quality/baseline_live.json` | First-ever judged quality baseline of the voice path (raw). |
| `docs/latency_traces_real.jsonl` | Raw per-turn traces through the live path. |

---

## The headline

**Every live voice turn and every session greeting raised `TypeError` and produced zero audio.**
The companion answered with silence, on every turn, with no fallback path.

```
File "voice/session.py", line 667, in _speak_turn
  result = await self._generator.generate_spoken(
TypeError: LangGraphOrchestrator.generate_spoken() got an unexpected keyword argument 'temperature'
```

Swallowed by the broad `except Exception` in `VoiceSession._run_turn_inner` as
`"voice turn failed"`. VAD, endpointing, STT, assembly and routing all worked — the transcript was
produced and stored. Only the reply never came.

**Attribution** (the "LangGraph migration" hypothesis was half right):
- `447016f` (LangGraph migration, Jul 7) wired `generator=pipeline.orchestrator` into a parameter
  still typed as the concrete `ResponseGenerator`. Duck-typing hid it. **This created the hazard.**
- `3182dd6` (open-greeting variety, Jul 9 — the most recent voice commit) added
  `temperature=temperature` to that call. **This broke it.** Hours old, not months.
- mypy *was* reporting it at `api/routes/voice.py:147`, buried in 205 pre-existing errors.

---

## What was done, per item

### F1 — Reproduced ✅
`scripts/voice_live_probe.py` drives the real entrypoint (real Silero VAD → real endpointing → real
Grok STT → wired engine → real Grok TTS). Before: `AUDIO OUT: 0 chunks, 0 bytes`, one swallowed
`TypeError`, `[error]` trace event. The greeting failed identically.

### F2 — Fixed at the contract ✅
`temperature` is now part of the `Orchestrator` **port**; both engines honour it; `VoiceSession`
depends on the port, not one concrete engine. mypy 205 → 204.
After: **8 chunks / 139,976 bytes**, 0 exceptions, graph nodes
`perceive → resolve_context → respond → reflect_log`, and `"when do I take my meds?"` →
*"You take your blood pressure medication every day around 8 PM."*
The live path genuinely runs through `LangGraphOrchestrator`.

### F3 — Programming errors now fail loudly ✅
`core/errors.py` splits **our bugs** (re-raise, full traceback, failed step in the trace) from
**dependency failures** (degrade, say so honestly, keep the conversation alive).

Four more instances of the same disease, all found while writing the tests:
- `converse()` discarded the consumer task's exception via `gather(return_exceptions=True)`
- `_consume()`'s `finally` did the same to the in-flight turn task
- STT is called from `_consume`, **outside** `_run_turn_inner`'s guard
- `merge_conversation` (`api/streaming.py`) swallowed it one layer further up
- and `tests/real_call/conftest.py` turned a wiring bug into `pytest.skip`

`assert_orchestrator_contract()` now runs in `build_pipeline` — a mis-wired engine crashes at
startup, not at every turn.

**Proven both ways on the real path:** an injected `TypeError` propagates out of `converse()`,
logged with a traceback, `programming_error=True` in the trace. A simulated total LLM outage
degrades to **128,736 bytes of honest audio** and the session survives.

### F4 — The test estate now drives the real entrypoint ✅

| Suite | What it drove *before* |
|---|---|
| `tests/real_call/*` (16 files) | `orchestrator.generate(...)` — the **text** path |
| `tests/golden/test_gs3_judge.py` | **canned reply strings** — never engine output |
| `scripts/latency_trace_capture.py` | a 4-positional-arg shape the voice edge never uses |
| **anything** | **`VoiceSession` — nothing** |

To be fair: the text-path tests are not worthless — `api/routes/chat.py` really does call
`orchestrator.generate()`. The defect is that **the voice path had zero real-call coverage.**

New: `scripts/live_turn.py` (shared driver), `tests/real_call/test_live_voice_path.py` (5 tests),
`say_voice()` on `RealTurns`, and both harnesses rewritten onto the live path.

**Regression guard proven by reintroducing the bug:** unit test failed in 0.57 s; all 5 real-call
voice tests **errored** (not skipped). Restored → green.

### F5 — SRC1 re-diagnosed: three defects, both hypotheses half right ✅ (diagnosed, not fixed)
1. **Routing gap (real).** `_is_live_info_query("...LTP of OP?") == False`. Proven by an A/B on
   `SYPNL`, a ticker the user *does* hold: `"LTP of SYPNL"` → 0 searches; `"price of SYPNL"` →
   1 search → correct live price. Same fixture, phrasing alone decides.
2. **Fixture gap (real).** `u_demo_001` has one entity ("My portfolio") and no OP anywhere.
3. **New defect nobody predicted.** With OP correctly seeded, `_capability_repair` still sends the
   raw utterance to Serper → answers with the **crypto** token. A perfect fixture still fails.
4. The ambiguity guardrail **never fired** (`action="respond"`, not `"disambiguate"`).

### F6 — True baseline, latency and quality ✅

**Latency** (through `VoiceSession`, frames paced at wall-clock rate like a browser mic):
first audio is **7.3–11.1 s**, not the 4.6–5.4 s reported. Three costs were invisible:

| Cost | Measured | Note |
|---|--:|---|
| TTS websocket handshake | 870–1014 ms | **critical path, every turn** |
| endpointing pause | ~700 ms | never counted before |
| `vocab.terms_for()` (Neo4j) | 426 ms | executed *inside* the STT span |

`context_intent` took **1872 ms on one turn and 6985 ms on the next** — same model, same tier.
Single-sample before/after comparisons measure noise.

**Quality** — first time the voice path has ever been judged. **It fails.**
3 of 11 scenarios come back `chatbot_like=true`, and the engine's own `style_flags` detector catches
none of them. Dynamic-tone gate fails (`min_tone_fit = 2`).

---

## The most important finding (not fixed — needs its own task)

`generate_spoken → _stream_reply → _finish_spoken → _finish`. The behaviour gates live in
`_finalize`, which **the streaming voice path never reaches.** So a spoken turn never runs:

- **self-reflection (§9.3)** — verified: *no `reflection` span exists on any voice turn*
- the curiosity gate · `check_boundary()` · `_warm_disclosure()` (§1.2 rule 4)

This violates CLAUDE.md §2 and §9 for **every spoken turn**, and explains the cold
*"While I don't feel emotions like a person does"* on `"do you actually care about me?"`.
Captured as item 0 in `docs/NEXT_CORRECTNESS_TASK.md`.

---

## Also true, and previously unstated

- **Prosody is never dynamic in production.** `settings.ser_service_url` is empty →
  `prompt.emotion` is always `None` → `read_register()` always returns `"neutral"`. The
  "falls back to text-sentiment" claim in three docstrings **has no implementation.**
- **Nothing has ever scored production quality.** `settings.langfuse_eval_enabled = False`.
- Prior claims verified only on a non-live path are now **unverified** — the full list is in
  `docs/TEST_REPORT.md` §F1–F6. Notably **L3 ("`context_intent` is skipped on simple turns") is
  false on the voice path**, and **U8 ("dynamic prosody ✅") never fires**.

---

## Checks

`ruff` clean · `mypy` 204 errors (**down** from the 205 baseline) · `lint-imports` 2/2 kept ·
**390 unit + 15 e2e + 5 real-call voice** tests pass.

6 acceptance tests fail with `ProfileNotFound` (a missing seeded profile). **Identical at
`c3dfd1d`** — pre-existing, not an isolation leak, not caused by this work.

## Deploy — the one human step

`deploy/update.sh` runs **on the server**, as root. This workstation has no `/opt/companion`, no
`companion-api` unit, and no SSH host configured, so it cannot be run from here.

```
sudo bash /opt/companion/deploy/update.sh
```

Worth doing promptly: this ships a fix for a total voice outage.
