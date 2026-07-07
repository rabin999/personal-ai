# Session Report — Post-Task Correction Audit (pass 2)

**Date:** 2026-07-07
**Method:** every claim verified by a **real chat turn** (real OpenRouter LLM + real Serper
search + real Mongo/Qdrant/Neo4j/Redis) before touching code. No mocks. Each fix re-verified by
a captured real conversation. Commits: `d91c2ed` (fix bundle), `c6be9d9` (remediation log).

---

## TL;DR

Audited the reported bugs against real runs. **Graphiti recall was NOT broken** (the audit's
suspicion was stale). **Five things were genuinely broken and shipped anyway** — TTS tags leaking
into chat, transient states stored as durable facts, unknown terms refused, "top N news" empty,
and the big one: **capability false-refusals** ("I don't have access to real-time data") on a
whole class of live-info queries. All five are now fixed and re-verified live.

The root reason these shipped: **BUILD_STATUS.md / CLAUDE.md §6 claim `real_call`/e2e coverage
everywhere, but there are zero `@pytest.mark.real_call` tests in the repo.** The mocked tests
passed while the real app gave bad answers — exactly the failure mode the brief warns about.

This was a focused, high-value bundle. **Most of the 11-part audit remains unverified/undone** —
see "Not done" below. Brutally honest scope: ~1 of 11 parts substantially closed.

---

## Part 0 — verification table (what a real run actually did)

| Item | Was claimed | Real run BEFORE | Verdict | State now |
|---|---|---|---|---|
| 1.1 Graphiti semantic recall | done | "takes BP meds daily ~8pm" **was** retrieved into the prompt | **works — audit stale** | unchanged |
| 1.3 "top N news" = N items | done | follow-up → "The first is about , and the second is about ." | **claimed-but-broken** | fixed (inline search) |
| 1.4 TTS tags in chat | done | `[sigh]` `<pause>` `[warm]` literally in chat text | **claimed-but-broken** | **fixed** |
| 1.5 / 8.13 transient→durable | done | "headache right now" stored as **semantic fact** | **claimed-but-broken** | **fixed** |
| 1.6 unknown term → search | done | "Herak" → "not sure I've heard of that" | **claimed-but-broken** | **fixed** |
| 8.8 capability awareness | (new) | "current time in Nepal" / weather → "I don't have access to real-time data" | **claimed-but-broken (class of bug)** | **fixed** |

---

## Fixes (each proven by a captured real conversation)

### 1. Capability awareness / tool routing — §8.8, §1.6, §1.3 (the class-of-bug)

**Root cause (three compounding faults):**
1. The identity/system prompt described the companion ("you remember past conversations") but
   **never stated it can search the web** — so the fast tier fell back to "I'm an AI, I can't
   access real-time data."
2. `web_search` is a **background** tool — even when the model *did* request it, the dispatcher
   enqueued it and returned a handle, so the turn produced a hollow "just a moment…" and **never
   answered**. The later follow-up then hallucinated empty items.
3. No deterministic net caught the refusal.

**Fix (three layers):**
- **System prompt:** an explicit *"What you can actually do"* block in `_identity_section`
  (`core/reasoning/prompt_assembly.py`) that lists real capabilities and **forbids** the
  "can't access real-time / never heard of it" refusal class.
- **Inline resolution:** `ToolDispatcher.run_inline()` (new) executes a tool synchronously,
  bounded (8 s). The response loop resolves a `web_search` request **in-turn** so the model
  answers with real data now; on timeout it falls back to the background/waiter path.
- **Deterministic backstop:** `_is_live_info_query()` (weather/news/time/price/score…) +
  `_needs_capability_refusal/_hollow_promise` force a real search and re-answer when the model
  ran no tool. Careful topic-marker regex so ordinary feelings never trip a search.

**Captured proof:**
```
"what is the weather in Kathmandu right now?"
→ "Right now in Kathmandu, it's 81°F and feels like 87°. You can expect occasional
   thunderstorms today with a high of 83° and a 70% chance of precipitation."
   [trace: web_search span tool_type=background:inline; log: POST google.serper.dev 200]

"what is Herak?"
→ "'Herak' can refer to a Bosnian Serb soldier named Borislav Herak, a donor to Gonzaga
   University named Donald Herak, or a Klingon warrior from Star Trek…"   (was: "never heard of it")

REGRESSION GUARD — "I feel kind of lonely today"
→ "I'm really sorry you're feeling a bit lonely today. Wanna talk about it?"
   serper calls before=2 after=2  → NO spurious search
```

**Logged design deviation (REMEDIATION_LOG R13):** spec §8.6/§15 says search is *always*
background (voice-latency reason). For explicit current-info questions we now resolve it inline
so the companion actually answers instead of promising a result that never arrives (§8.8/§8.11).
Background/waiter path retained as the timeout fallback and for the voice runtime.

### 2. TTS tags stripped from chat, preserved in trace — §1.4

`GenerationResult` now carries **two** fields: `final_text` (ALL delivery tags stripped — for the
chat UI and stored memory) and `voice_text` (whitelisted tags kept — for TTS + shown raw in the
trace). `_strip_all_tags()` cleans chat text; `_sanitize_tags()` keeps whitelisted tags for voice.
`api/routes/chat.py` and `voice/session.py` updated so **TTS speaks `voice_text`** (prosody
preserved) while chat + memory get clean text.

**Captured proof:**
```
"I just got the job I really wanted!"
CHAT REPLY:  "Oh, Nandi, that's fantastic news! Congratulations, I'm so happy for you!"   (0 tags)
TRACE voice_text: "Oh, Nandi, that's fantastic news! [warm] Congratulations, I'm so happy for you!"
```

### 3. Transient state → episodic, not durable fact — §1.5, §8.13

Extraction prompt (`core/memory/extraction.py`) now explicitly separates durable facts /
preferences / routines from transient current states, and a deterministic `_looks_transient()`
guard demotes any transient "fact" to **episodic-only** (with a durability-marker allowlist so
"daily/every/prefers/works at" always stay semantic).

**Captured proof:**
```
"I have a headache right now"          → stored 1 event(s), 0 fact(s)   (was: 1 fact)
"I go for a run every morning at 6am"  → stored 1 event(s), 1 fact(s)   (durable fact kept)
```

---

## Trace completeness (spot-checked, §5 backend only)

The durable per-turn trace is more complete than the brief feared. The weather turn recorded:
`session · retrieval · assembly · router · judgment · generation · 6× llm.call · 2× tool ·
response(+voice_text)`, grouped by session, at `/debug/traces/{session}`. The `web_search` tool
span carries `tool_type=background:inline` + the result summary; the `response` span carries the
raw tagged `voice_text`. **Not verified:** the clickable list→detail **UI** (Part 5) — only the
backend data.

---

## Tests & checks

- New deterministic guards: `tests/unit/test_audit_fixes.py` (tag stripping, live-info routing,
  refusal detection, transient classification).
- Corrected two tests that pinned the **old buggy** behavior: the golden tag test (now asserts
  clean `final_text` + tagged `voice_text`), and the prompt-size ceiling (raised to account for
  the always-present, non-trimmable capability block).
- **Full non-real-call suite: green** (exit 0, 0 failures). `mypy` clean on changed files.
  `lint-imports` clean (core ↛ adapters intact).
- Note: `tests/unit/test_pipecat_processor.py` fails to *collect* (pipecat optional extra not
  installed) — pre-existing, unrelated to these changes.

---

## Not done / still unverified (the honest gap)

These were **not** addressed; they remain as previously claimed, unverified by me:

- **Part 3** — unified structured result envelope.
- **Part 4** — real LLM-as-judge suite. **Still does not exist.**
- **Part 5** — trace **UI** (list → click → detail with metrics/prompt-version/judge/feedback).
  Backend data is largely present; the UI was not driven.
- **Part 6** — prompt versioning + version-grouped performance attribution + prompt caching.
- **Part 7** — conversation behaviors (session timeout→consolidation, offer-once/comfortable-
  with-silence, heavy-mood re-engagement, correction supersede, waiter delivery/carry-over/
  pileup). Unverified.
- **Part 8** (except 8.8 / 8.13) — graceful degradation, cost-ceiling enforcement, memory
  conflict supersession, ambiguous-entity guardrail, feedback→trace linkage, barge-in
  continuity, engine/model switch persistence, streaming voice input, acknowledge-first-then-
  parallel, latency levers. Unverified.
- **Part 9–11** — deeper agentic loop, doc-contradiction cleanup, and the broader real-test
  replacement. Only the audit-fix tests above were added.
- **Voice path** (Pipecat migration, barge-in immediacy, streaming STT, SER GPU) — needs a mic /
  GPU; not verifiable in this environment.

---

## Biggest remaining gap

**Existing memory pollution is not cleaned.** The live prompt for `u_demo_001` still contains
junk accreted from earlier broken runs — a hallucinated fact *"Sundari believes a dark room will
help them"* and ~6 near-duplicate "asking about Japan/Korea" episodic entries. My fixes stop
**new** pollution, but nothing consolidates/dedups/supersedes the **existing** store
(Parts 8.3 conflict-supersession, 8.4 consolidation). That is the highest-value next bundle.

Secondary: with zero `real_call` tests, every "done" in BUILD_STATUS is unverified. The judge
suite (Part 4) + a handful of real-call behavior tests would have caught all five bugs above
automatically and should come before more feature work.

---

## Files changed

```
core/reasoning/prompt_assembly.py   capability block in the system prompt (§8.8)
core/reasoning/response_gen.py      final_text/voice_text split; inline-search + backstop
core/tools/dispatcher.py            ToolDispatcher.run_inline (bounded in-turn tool exec)
core/memory/extraction.py           transient-vs-durable prompt + _looks_transient guard
api/routes/chat.py                  trace keeps voice_text; chat returns clean text
voice/session.py                    TTS speaks voice_text; memory/trace get clean/raw
tests/unit/test_audit_fixes.py      NEW deterministic guards
tests/golden/test_gs3_behavioral.py corrected tag contract
tests/unit/test_prompt_assembly.py  raised size ceiling for capability block
docs/REMEDIATION_LOG.md             R13–R16
```

**Note:** `today_report.md` had been truncated to empty in the working tree at session start
(not by this session's edits) — it was restored from git, then replaced with this report.
