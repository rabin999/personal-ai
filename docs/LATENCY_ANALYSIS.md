# Latency & Waste Trace Analysis

**Measurement task — no optimizations applied.** Four real turns captured against the real
engine (real OpenRouter models, real Mongo/Qdrant/Neo4j/Redis, real Serper, real Grok STT/TTS).
Raw per-turn traces: [`docs/latency_traces.jsonl`](latency_traces.jsonl). Capture harness:
`scripts/latency_trace_capture.py`.

Every number below is measured. Judgments about necessity/redundancy are labelled **HYPOTHESIS**
and kept separate from **FACT**.

---

## 0. Method & what "turn start" means (read this first)

- Each turn runs through the **real voice reply path** (`orchestrator.generate_spoken`) — the same
  entrypoint the live WebSocket uses — with the structured logger bound so every per-LLM-call span
  (`start_ts`/`end_ts`/tokens/cost/cache/purpose) persists to `turn_traces`.
- **STT** is measured on the real Grok STT by synthesizing each prompt with the real Grok TTS
  (24 kHz), resampling to 16 kHz, and feeding the VAD-bounded clip to the adapter — measuring
  end-of-audio → final transcript. The transcript is reported (accuracy is itself a finding).
- **TTS** first-audio is measured by synthesizing each spoken sentence on the real Grok TTS and
  timing the first returned PCM chunk.
- **Clock convention:** the reasoning waterfall offsets (`start_ms`/`end_ms`) are relative to
  **end-of-STT** (the moment the user stops speaking and STT begins is `0`; STT occupies
  `[0, stt_gap]`; the reasoning path is measured from a fresh `p0` immediately after). So the
  **full user-perceived wait = `stt_gap_ms` + `total_e2e_ms`**, and **absolute time-to-first-audio
  = `stt_gap_ms` + `time_to_first_audio_ms`**. This is stated because the harness resets its clock
  after STT; the JSONL records both parts.

### Headline numbers (measured)

| Turn | Shape | STT gap | Reasoning e2e | **Full wait** | **Abs. first-audio** | LLM calls | Tool calls | Cost |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| 1 | trivial `hi` | 604 ms | 5480 ms | **6084 ms** | **5437 ms** | 2 | 0 | $0.00133 |
| 2 | memory recall | 745 ms | 5060 ms | **5805 ms** | **4642 ms** | 2 | 0 | $0.00149 |
| 3 | live search | 950 ms | 7624 ms | **8574 ms** | **4942 ms** | 2 | 0 | $0.00091 |
| 4 | multi-intent | 1542 ms | 26082 ms | **27624 ms** | **4773 ms** | 10 | 4 searches | $0.01146 |

First audio lands at a fairly constant **~4.6–5.4 s** across all shapes. The multi-intent turn's
27 s is the *tail* — it keeps searching and re-speaking for ~22 s after the first sentence.

---

## 1. Per-turn timeline / waterfall

### Turn 1 — trivial `"hi"` → *"Hey Nandi, good to hear from you again! What's on your mind today?"*
```
   0 ──604   STT (batch, 0.51s audio)            604 ms
   0 ─1331   prompt_assembly  (COLD START)      1331 ms   ← first-turn warmup, see §6
1334 ─3335   LLM context_intent  gemini-flash   2002 ms   ← fired on "hi"  (HYPOTHESIS: waste)
3339 ─5432   LLM response        gemini-flash   2093 ms   in=3185 out=19
5472 ─5480   memory_write (inline)                 8 ms   (routing DEFERRED to worker)
```
Two sequential LLM calls for a greeting. No overlap between them.

### Turn 2 — memory recall `"when do I take my meds?"` → *"You take your blood pressure medication every day around 8 PM."*
```
   0 ─ 745   STT (1.69s audio)                    745 ms
   0 ─ 137   prompt_assembly                      137 ms
 139 ─1847   LLM context_intent  gemini-flash    1708 ms
1855 ─2981   LLM response        gemini-flash    1126 ms   in=3539 out=13
5057 ─5060   memory_write (inline)                  3 ms
```
Correct recall (real fact from memory). Still two sequential calls.

### Turn 3 — live search `"what's the current LTP of OP?"` → *"I'm sorry, Nandi, I don't have enough information to get you the current LTP for 'OP.'…"*
```
   0 ─ 950   STT (2.57s audio)                    950 ms
   0 ─ 100   prompt_assembly                      100 ms
 102 ─1709   LLM context_intent  gemini-flash    1608 ms
1719 ─6047   LLM response        gemini-flash    4328 ms   in=3389 out=36  (cache HIT)
7622 ─7624   memory_write (inline)                  2 ms
```
**No web_search fired (0 tool calls).** A live stock-price query punted instead of searching — see §5/§10.

### Turn 4 — multi-intent → *"Nandi, I'm checking on the current weather in Kathmandu and what OP … is trading at…"*
```
   0 ─1542   STT (5.99s audio)                                    1542 ms
   0 ─ 183   prompt_assembly                                       183 ms
 187 ─1498   LLM context_intent                                  1311 ms
1516 ─3266   LLM response #1 (requests a search)                 1750 ms  ← draft DISCARDED
1505 ─5113   LLM ack (filler, CONCURRENT with response #1)       3608 ms  ← overlaps, but 3.6s
3271 ─6617   web_search #1  HTTP                                 ~3346 ms
5079 ─6609   LLM search_summarize #1                             1530 ms
6631 ─8409   LLM response #2 (requests ANOTHER search)           1778 ms  ← draft DISCARDED
8414 ─11541  web_search #2 + summarize #2                        ~3128 ms
11558─13787  LLM response #3 (requests ANOTHER search)           2229 ms  ← draft DISCARDED
13791─16659  web_search #3 + summarize #3                        ~2868 ms
16675─18495  LLM response #4                                     1820 ms
18496─21214  web_search #4 + summarize #4                        ~2718 ms
21221        judgment + reflection (draft had banned filler)
           → total 26082 ms
```
Four **sequential** search→summarize→response cycles. Three intermediate response drafts (177/181/184
output tokens) were discarded because each re-requested a tool.

---

## 2. Top bottlenecks by measured time

**Overall (across the 4 turns):**
1. **Sequential ReAct tool loop (Turn 4): ~24 s.** Four search+summarize+response cycles run
   one-after-another. Dominates everything else by an order of magnitude.
2. **`context_intent` pre-step: 1.3–2.0 s on *every* turn** (2002 / 1708 / 1608 / 1311 ms) — including
   trivial `"hi"`. It is the single largest fixed cost on simple turns.
3. **`response` generation: 1.1–4.3 s per call**, driven by large input prompts (3185–5663 tokens)
   and near-total cache misses (only 1 of 15 response/intent calls was a cache hit).
4. **STT batch gap: 0.6–1.5 s**, scaling with utterance length (batch mode, §4).
5. **`prompt_assembly` cold start: 1331 ms on Turn 1 only** (100–183 ms thereafter).

**Per simple turn (1 & 2),** the first-audio wait ≈ STT + assembly + context_intent + first response
sentence; **context_intent alone is ~30–40 %** of that wait.

---

## 3. Sequential steps that could run in parallel (measured durations)

- **Turn 4's 4 searches (weather vs. OP are independent intents):** searches at
  `3271→6617`, `8414→11541`, `13791→16659`, `18496→21214` — **all sequential**, ~12 s of wall time
  in the searches alone, plus the 4 response regenerations between them. Two independent intents
  could dispatch **2 searches concurrently** instead of 4 serial. *(This is the L2 gap, quantified.)*
- **`context_intent` (1.3–2.0 s) runs strictly before `response`** on every turn. They are sequential
  by construction (intent feeds the prompt). If context_intent stays, it cannot overlap response; if
  it were skippable on simple turns, ~2 s is removed outright. HYPOTHESIS.
- **`ack` already overlaps** the first response+search on Turn 4 (`1505→5113` vs `1516→3266`) — this is
  the one place parallelism is working. (But the ack itself took 3.6 s; see §5.)

---

## 4. The STT gap (FACT)

| Turn | Audio length | End-of-speech → final transcript | Interim partials |
|---|--:|--:|--:|
| 1 | 0.51 s | 604 ms | 0 |
| 2 | 1.69 s | 745 ms | 0 |
| 3 | 2.57 s | 950 ms | 0 |
| 4 | 5.99 s | 1542 ms | 0 |

**Mode: BATCH.** `settings.stt_engine = "grok"` → the adapter waits for the whole VAD-bounded
utterance, then makes **one HTTP round-trip** to xAI STT. **Zero interim partials** were emitted, and
the gap grows with utterance length — both confirm batch/segmented, not streaming. The
local `faster-whisper` adapter *does* emit streaming partials, but it is not the configured engine.
Implication: for a 6 s utterance the user eats a fixed ~1.5 s *after* they stop talking before
anything downstream can begin.

---

## 5. LLM call inventory (FACT + necessity HYPOTHESES)

| Turn | # calls | Purposes | Discarded output? | Retried? | Cache hits |
|---|--:|---|---|---|---|
| 1 | 2 | context_intent, response | none | no | 0/2 |
| 2 | 2 | context_intent, response | none | no | 0/2 |
| 3 | 2 | context_intent, response | none | no | 1/2 (response) |
| 4 | 10 | context_intent, response×4, ack, search_summarize×4 | **3 response drafts** | no (each requested a *new* tool) | 0/10 |

**Facts:**
- **15 total LLM calls across 4 turns; every turn pays a `context_intent` call** (1.3–2.0 s).
- **Turn 4 discarded 3 `response` drafts** (steps #2/#3/#4 = 1778+2229+1820 ≈ **5.8 s** of generation
  whose output never reached the user — each was superseded when the model asked for another search).
- **Cache is almost never hitting:** 1 hit in 15 calls. The Turn-4 loop's inputs grow every step
  (4478 → 4827 → 5134 → 5663 tokens) and none cached, so it re-pays the full prompt each cycle.
- **The `ack` call took 3608 ms** (in=3492 tokens) — it *did* overlap the search (good), but a
  "throwaway filler" using the full assembled prompt is not cheap.
- **No silent retries** were observed in these 4 turns (the JSON-validation retry path did not fire).

**Hypotheses (verify before acting):**
- **context_intent on `"hi"` (2002 ms) is unnecessary** for a trivial greeting. BUILD_STATUS claims
  context_intent is gated on simple turns (L3/L5), but the trace shows it firing on every turn
  including `"hi"` — so either the gate isn't catching trivial turns or it runs pre-classification.
  **Flag: verify the simple-turn gate actually short-circuits context_intent.**
- **Turn 4's 4 responses + 4 searches overlap in reasoning** — the model re-answers the whole
  multi-intent request after each search rather than gathering both facts first. Redundant
  re-generation.
- **context_intent may duplicate work the `response` step redoes** (both consume the assembled memory
  context). Possible overlapping reasoning.

---

## 6. Concurrent / background work

**Observed in the captured window:**
- **`ack` ran concurrently** with response #1 + search #1 on Turn 4 (asyncio) — real parallelism, no
  blocking.
- **Inline memory write is 2–8 ms** every turn: only `working.append` + the raw conversation-log
  write run on the turn. **Memory routing/extraction is DEFERRED** (`defer_memory_routing = True`) to
  the background worker — correctly *not* inline. Good.

**Known background jobs NOT triggered by this harness (from code, `voice/session.py` + composition):**
- **Judge/evaluator** (`TurnEvaluator.schedule`) fires a **background `asyncio.create_task` per turn**
  that runs a *separate judge LLM call* (`judge_companion_voice`). It is off the reply path (not
  awaited) but **shares the event loop and the same OpenRouter client** — so on the live path it
  competes with the next turn's calls. **Not captured here** (the harness calls `generate_spoken`
  directly, bypassing `VoiceSession` which schedules it). Listed under Missing Instrumentation.
- **Post-session consolidation** is enqueued to the worker at session end — off the turn path.

No evidence of a background job *blocking* the turn; the open risk is event-loop / LLM-client
contention from the per-turn judge, which this capture could not measure.

---

## 7. Blocking work that should be streamed

- **`context_intent` fully blocks** before any reply token — the user waits its 1.3–2.0 s with nothing
  emitted.
- **The multi-intent tool loop is fully blocking between sentences:** after the first holding line, the
  user waits ~5 s per additional search cycle with silence, ×4.
- **Response generation *is* streamed** on the reply path (first sentence at
  `time_to_first_sentence_ms` ≈ 2.4–4.1 s post-STT) and **TTS starts on the first sentence**
  (`time_to_first_audio_ms` follows shortly after) — these are already progressive. **Good.**
- **`search_summarize`** blocks the follow-on response (the model can't answer until the summary
  returns) — inherent, but the 4 sequential summaries compound it on Turn 4.

---

## 8. Waste summary (required vs. actual)

| Turn | LLM calls (actual) | Strictly required (HYPOTHESIS) | Tool calls (actual / needed) | Measured wasted / recoverable time |
|---|--:|--:|--:|--:|
| 1 `hi` | 2 | **1** (response) | 0 / 0 | **~2002 ms** (context_intent) |
| 2 recall | 2 | 1–2 | 0 / 0 | up to ~1708 ms (context_intent, if skippable) |
| 3 search | 2 | 2 (but *should* have searched) | 0 / **1** | search missing → wrong answer, not slower |
| 4 multi | 10 | ~4 (2 searches + ~2 responses) | 4 / **2** | **~5.8 s** discarded response drafts + **~6–9 s** from serial (vs parallel) searches |

**Estimated recoverable time (measured components, upper bound):**
- Skip `context_intent` on trivial/simple turns → **~2 s** off Turns 1–2 first-audio.
- Parallelize Turn 4's independent searches + stop re-generating discarded drafts → **~12–15 s** off
  the multi-intent turn (from ~26 s toward ~11–14 s).
- Streaming STT instead of batch → **~0.6–1.5 s** off the post-speech gap per turn.

---

## 9. Missing instrumentation (first-class findings — these could NOT be measured today)

1. **Token-level TTFT is not recorded.** The stream's *first-delta* time is not surfaced per turn;
   only *first spoken sentence* (first `speak()` call) is observable. Reported `time_to_first_sentence_ms`
   is a proxy, not true TTFT.
2. **Trace events are point-in-time (`ts` only) — no per-stage `start`/`end`/`duration`** except for
   LLM calls (which do log `start_ts`/`end_ts`). Stage durations for STT/assembly/etc. had to be
   derived or measured by the harness, not read from the trace.
3. **VAD and endpointing latency are not captured** in the reasoning/text path. They live only in the
   live `VoiceSession` frame loop (`voice/session.py`) and emit no timed spans — so "how long did the
   semantic endpointer wait to decide the turn ended" is not in the trace. (The endpointer's
   *configured* pause windows are known; its actual decision latency is not recorded.)
4. **Sentiment/prosody (SER) has no per-turn latency span.** Emotion is read one turn behind
   (`LaggingEmotionProvider`); there is no measured SER inference time.
5. **The per-turn judge/evaluator LLM call is not traced on the turn** (background task, different
   correlation) — its model, latency, and cost, and its event-loop contention with the live turn,
   are unmeasured.
6. **web_search internal split is not isolated.** The tool span records request/result timestamps,
   and `search_summarize` is a separate LLM span, but the **raw Serper HTTP latency** vs. cache-read
   vs. summarize is not individually timed (inferred from the gap between the tool `request` span and
   the `search_summarize` call).
7. **Prompt-cache accounting is per-call but there is no turn-level "how much of this prompt was
   cacheable but missed" signal** — we can see `cache_hit=false`, not *why* it missed.

---

## 10. Recommended next actions, ranked by measured impact (DO NOT implement in this task)

1. **Gate `context_intent` off trivial/short turns** — measured ~2.0 s on `"hi"`, ~1.3–1.7 s on every
   turn. Highest, most consistent win on the common case. *(Verify the existing "simple-turn gate"
   first — the trace shows it firing when it allegedly shouldn't.)*
2. **Multi-intent: dispatch independent tool calls in parallel and stop regenerating discarded
   response drafts** — measured ~5.8 s of discarded generation + ~6–9 s of serial-vs-parallel search
   on Turn 4. Largest absolute win, but only on multi-tool turns.
3. **Fix the live-search trigger** — Turn 3 (`"LTP of OP"`) never searched and punted. This is a
   correctness bug surfaced by the latency capture, not a speed win, but it defeats the whole
   live-search path.
4. **Make the `ack` cheap** — 3.6 s with a 3492-token input for a throwaway one-liner; a minimal
   prompt would cut it (it overlaps the search, so impact is on cost/contention more than wall-time).
5. **Investigate prompt-cache misses** — 1 hit in 15 calls; the growing multi-intent context never
   caches. Caching the stable system prefix across the ReAct loop would cut repeated input cost/latency.
6. **Consider streaming STT** (faster-whisper partials, or a streaming Grok mode) — ~0.6–1.5 s per turn
   off the post-speech gap; trade-off vs. Grok batch accuracy/cost needs its own evaluation.
7. **Add the missing instrumentation in §9** (token-level TTFT span, per-stage start/end, VAD/endpoint
   timing, SER latency, judge-call tracing) so the next round can measure what this one had to infer.

---

*Captured 2026-07-09 · 4 real turns · `scripts/latency_trace_capture.py` → `docs/latency_traces.jsonl`.*
