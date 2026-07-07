# Today Report — Status Audit (evidence-backed)

> **Update (same day, after the audit):** the two memory bugs this audit found are now
> fixed and re-verified live — (1) **Mem0 is wired** (preference layer: WRITE in the
> extraction step, READ in prompt assembly; OpenRouter+fastembed+Qdrant), (2) the
> **double-write on recall is gone** (recall turns now store nothing; ledger stays at
> `entry_count: 1 / 2300`), and (3) **semantic retrieval returned the fact** on the
> re-run (`'user takes blood-pressure prescription daily at 8pm'`). Details in
> `docs/REMEDIATION_LOG.md` (R11–R12). The audit findings below are preserved as the
> point-in-time record.

---

## Original audit (point-in-time)

**Date:** 2026-07-07 · **Method:** greps, dependency inspection, and a **real captured
conversation** against the live pipeline (OpenRouter + docker-compose datastores). Every
claim below has a file path or command output. Where something isn't done, it says so.

---

## 1. Proper tools — installed AND actually wired?

| Tool | In deps? | Wired in code? | Verdict |
|---|---|---|---|
| **Graphiti (+Neo4j)** | ✅ `graphiti-core>=0.29.2`, `neo4j>=6.2.0` | ✅ imported `adapters/db.py:17`, `adapters/graph/graphiti.py`; used by `core/memory/semantic.py` | **Wired, but not functioning for retrieval** — see §3: `profile_facts` returned `(none)` and the run logged `Source entity not found in nodes for edge relation: TAKES`. The engine is real; the temporal facts aren't coming back. |
| **Qdrant** | ✅ `qdrant-client>=1.18.0` | ✅ `adapters/db.py:26`, `adapters/vector/qdrant.py`; used by `core/memory/episodic.py`, `entities.py` | **Wired and WORKING** — episodic retrieval surfaced the meds + trade across a new session (§3). |
| **Pipecat / LiveKit** | ✅ `pipecat-ai[silero]>=1.5.0` · LiveKit ❌ absent | ⚠️ Pipecat used **only for the Silero VAD model** (`adapters/vad/silero.py:28`). The VAD gate, endpointing, barge-in and session loop are **hand-rolled** in `voice/pipeline.py`, `endpointing.py`, `bargein.py`, `session.py`. No Pipecat transport / `PipelineTask` / `FrameProcessor`. | **Mostly hand-rolled.** Pipecat present but its pipeline/transport/barge-in is NOT used. |
| **Mem0** | ✅ `mem0ai>=2.0.11` (installed today) | ⚠️ Adapter written `adapters/preference/mem0_adapter.py` + port `ports/preference_memory.py`; **smoke-tested working** (extracted "User loves hiking", "User's dog is named Trishul" via OpenRouter+fastembed+Qdrant). **NOT yet imported by `api/composition.py` / prompt assembly.** | **Installed + adapter, NOT live.** Integration in progress. |
| **Langfuse / OpenTelemetry** | ❌ `grep -c langfuse uv.lock` → **0**; no opentelemetry | ❌ Only a comment in `core/observability/trace_store.py:13` explaining the choice NOT to use it. Tracing is hand-rolled (`voice/trace.py` + `core/observability/trace_store.py`, Mongo-backed). | **Not present.** Observability is hand-rolled (stage-level, see §3). |
| **OpenRouter** | ✅ (via `openai` client) | ✅ `adapters/llm/openrouter.py` | **Wired, WORKING** (all LLM calls in §3). |
| **Grok TTS** | ✅ (xAI via httpx) | ✅ `adapters/tts/grok.py` | **Wired**; audibility not verified (needs ears). |
| **Serper / Brave** | ✅ keys in `.env` | ✅ `adapters/search/serper.py`, `adapters/search/brave.py` | **Wired, WORKING** (news pulled in §3). |
| **Silero VAD** | ✅ (pipecat extra) | ✅ `adapters/vad/silero.py` | **Wired.** |
| **emotion2vec (SER)** | ✅ `ser` extra | ✅ `adapters/ser/emotion2vec_client.py` + `services/ser_service` | **Wired, NOT verified** (needs GPU service running). |
| **faster-whisper (STT)** | ✅ `faster-whisper>=1.2.1` | ✅ `adapters/stt/faster_whisper.py:49` | **Wired.** |
| **Pydantic** | ✅ | ✅ everywhere (LLM outputs, extraction, tools) | **Wired, WORKING.** |

---

## 2. Where the wheel was reinvented

| Hand-rolled | File(s) | Named tool it "should" be | Why |
|---|---|---|---|
| Voice pipeline: VAD gate, endpointing, barge-in, session loop | `voice/pipeline.py`, `voice/endpointing.py`, `voice/bargein.py`, `voice/session.py` | **Pipecat** (pipeline/transport/native barge-in) | Original build predates this session; this session chose to fix specific bugs (pre-roll, delivery) over a full Pipecat migration that can't be A/B-tested without a mic. Logged in `REMEDIATION_LOG.md`. **This is the biggest reinvention and the likely reason barge-in immediacy can't be guaranteed.** |
| Tracing / observability | `voice/trace.py`, `core/observability/trace_store.py` | **Langfuse / OTel** | Chose Mongo-backed trace to avoid standing up a Langfuse server; documented. Result: stage-level trace exists, but no hierarchical per-LLM-call token/cost/latency in the trace (cost is in a separate ledger). |
| Memory extraction / consolidation loop | `core/memory/extraction.py` | **Mem0** (`add()`/`search()`) | Built a custom Pydantic extraction step this session. Mem0 now installed to complement it but not yet wired. Some overlap. |
| Entity resolution, working memory, procedural rules, conversation log | `core/memory/entities.py`, `working.py`, `procedural.py`, `conversation_store.py` | (no named tool) | Legitimately custom; not a reinvention. |

---

## 3. What actually works — proven by conversation (verbatim capture)

Captured from a live run (real model + datastores). Raw, unedited:

```
[s1] USER: hi
  COMPANION: Hey there! So good to meet you. What do I call you, and what's up?
  style_flags: []                        ← warm companion, NOT assistant-speak ✅

[s1] USER: I take my blood-pressure prescription every day at 8pm.
  COMPANION: Got it. You take your blood pressure prescription every day at 8 PM.
  WROTE -> episodic:1 semantic:1 trades:0
    semantic facts: ['takes blood-pressure medication daily around 8pm']   ← distilled ✅

[s1] USER: record that I bought 10 units of SYPNL at 230
  COMPANION: Got it. You bought 10 units of SYPNL at 230.
  WROTE -> episodic:1 semantic:0 trades:1                                    ← ledger write ✅

--- NEW SESSION s2 ---
[s2] USER: hey, when do I take my medication again?
  RETRIEVED episodic: ['took blood-pressure prescription daily at 8pm', ...]
  RETRIEVED semantic facts: (none)                        ← Graphiti returned NOTHING ⚠️
  COMPANION: You take your blood pressure prescription every day at 8 PM.   ← RECALL ✅ (via episodic)

[s2] USER: what did I buy recently?
  COMPANION: You recently bought 10 units of SYPNL at 230.                   ← RECALL ✅
  WROTE -> ... trades:1                                   ← RE-LOGGED the same trade ⚠️

LEDGER STATE: {... 'net_invested': 4600.0, 'entry_count': 2}   ← should be 2300/1 entry ⚠️

[web_search] top news
  summary: Trump...F-35 for Turkey. Russia attacks Ukraine ahead of NATO summit.
           Sri Lanka prison riot. Ebola deaths top 500 in Congo.
  distinct sources: 8 of 8                                ← distinct, no dup ✅ (but a paragraph, not a numbered "top 2")
```

**Scorecard for §3:**
- ✅ **"hi"** → real companion voice, warm/curious/short, no assistant-speak.
- ✅ **Store fact → new-session recall** WORKS — "when do I take my meds?" → "every day at 8 PM" in a fresh session.
- ✅ **Trade record → later recall** WORKS — "what did I buy?" → "10 units of SYPNL at 230".
- ✅ **Top news** distinct + once (8/8 distinct sources), BUT rendered as a summary paragraph, not a clean numbered "top 2" list.
- ❌ **Interruption / immediate barge-in** — NOT verifiable here (no microphone / duplex audio client in this environment). The cancel path exists in `voice/session.py` but real instant-halt depends on AEC + a live audio client. **Unproven.**
- ⚠️ **Two real bugs found by this run** (reported, not fixed): (1) **semantic/Graphiti retrieval returns nothing** — recall is currently carried entirely by episodic; (2) **recall turns re-extract** — when the companion restates a past trade, the extraction logs it AGAIN (ledger doubled to 2 entries / 4600).

**Observability trace (real, captured):**
```
session     | text turn started
retrieval   | episodic=0 hits
assembly    | complexity=simple           model_override=null
router      | tier=simple
generation  | action=clarify              style_flags=[]
response    | Hey! So glad to be here...
memory      | epi=1 sem=1 trades=0        facts=["user takes medication daily around 8pm"]
```
So observability IS real at **stage level** (read → assemble → route → generate → memory-write),
persisted to Mongo (`turn_traces`) and served at `/debug/traces`. What it does NOT have:
per-LLM-call token/cost/latency inside the trace, self-reflection as its own span, or Langfuse.
It shows *what happened*, not yet *how much each call cost*.

---

## 4. Module-by-module honesty (§1–§26)

`V` = verified by running it this session · `C` = code complete, NOT run-verified this session

| § | Module | State |
|---|---|---|
| §1 | Database layer | **working (V)** — Mongo/Qdrant/Neo4j/Redis connect; pipeline boots. |
| §2 | Config & profile | **working (V)** — traits seeded, model_prefs, audio_prefs. |
| §3 | Cost ledger | **working (C)** — logged per call; not eyeballed this run. |
| §4 | Working memory | **working (V)** — in-session turns used in prompt. |
| §5 | Episodic | **working (V)** — write + cross-session retrieval proven in §3. |
| §6 | Semantic (Graphiti) | **partial/broken (V)** — writes attempted, **retrieval returns none**; "Source entity not found" warnings. This is a real gap. |
| §7 | Procedural | **code complete (C)** — not exercised in §3. |
| §8 | Entity resolution | **working (C)** — used by assembly; not stressed this run. |
| §9 | Self-model / overclaim | **working (V)** — style/overclaim path ran (style_flags clean). |
| §10 | Prompt assembly | **working (V)** — assembled every turn; traits reach prompt. |
| §11 | LLM router | **working (V)** — tier routing + fast-model override live. |
| §12 | Response gen + gates | **working (V)** — real replies, gates, no assistant-speak. |
| §13 | Tool dispatcher | **working (C)** — ReAct loop; dedup guard added; web_search ran. |
| §14 | Background queue | **code complete (C)** — deferred delivery not exercised in §3. |
| §15 | Web search | **working (V)** — Serper returned distinct headlines. |
| §16 | Projects / ledger | **working (V) with a bug** — trade persisted + recalled, but recall turns double-log (§3). |
| §17 | Psych user-model | **code complete (C)** — not observed this run. |
| §18 | Learning/consolidation | **code complete (C)** — runs as queued task; not observed. |
| §19 | Audio input pipeline | **code complete, hand-rolled (C)** — pre-roll fix in; not audio-verified. |
| §20 | STT (faster-whisper) | **code complete (C)** — not run (no audio this session). |
| §21 | Endpointing | **code complete (C)** — logic only; not audio-verified. |
| §22 | SER (emotion2vec) | **stubbed-until-GPU (C)** — needs the GPU service. |
| §23 | TTS (Grok) | **code complete (C)** — voice preview endpoint added; audibility unverified. |
| §24 | Barge-in | **code complete, UNPROVEN (C)** — cannot verify instant halt without a mic. |
| §26 | User context (static auth) | **working (V)** — token→user resolution used throughout. |

**"Claimed done but not verified by running":** §3, §7, §8, §13(queue path), §14, §17, §18,
§19–§24 (all voice/audio + learning). The memory/reasoning/response/search core (§1,2,4,5,9,10,11,12,15,16,26) **is** run-verified this session.

---

## 5. Bottom line

- **What works right now (run-verified):** the text/reasoning core is genuinely good — warm
  companion voice (no assistant-speak), cross-session recall of facts and trades, live tool
  use, and a stage-level trace. Roughly **~60–65% of the specified app is actually working**
  end-to-end from the text boundary. The remaining ~35% is either **voice/audio (§19–§24,
  unprovable here without a mic)** or **learning/psych (§17–§18, built but not observed)**.

- **Reinvented that shouldn't be:** (1) the **voice pipeline / barge-in** is hand-rolled
  instead of Pipecat — the biggest one; (2) **tracing** is hand-rolled instead of Langfuse;
  (3) the **memory extraction loop** is custom while **Mem0 is installed but not yet wired**.

- **Single biggest thing between now and a working companion:** **the semantic-memory layer
  (Graphiti) is not returning facts** — recall is surviving purely on episodic vector search,
  and the extraction step double-writes on recall turns. Memory correctness (reliable
  semantic retrieval + not re-storing what's merely being recalled) is the #1 blocker. Voice
  barge-in via Pipecat is the #2 blocker, but it's unverifiable in this environment.

*(No code was changed in this audit. Mem0 was installed + an adapter written prior to this
audit request; it is not yet wired into the live loop.)*
