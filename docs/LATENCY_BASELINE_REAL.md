# Latency baseline — measured through the REAL live path

**This supersedes `docs/LATENCY_ANALYSIS.md` as the basis for any optimization work.**

That document measured `orchestrator.generate_spoken(prompt, dispatcher, ctx, speak)` — four
positional args, a call shape `VoiceSession` never makes. At the time it was captured, the live
voice path was raising `TypeError` on **every** turn and producing **zero audio**
(`docs/CODE_FLOW.md` §0). So it profiled a function the app could not reach.

Everything below is captured through `VoiceSession.converse` — the exact code
`api/routes/voice.py::voice_ws` runs. Real Silero VAD → real semantic endpointing → real Grok STT
→ the wired `LangGraphOrchestrator` → real Grok TTS. Real Mongo/Qdrant/Neo4j/Redis/Serper.

Harness: `scripts/latency_trace_capture.py` → `scripts/live_turn.py`.
Raw traces: `docs/latency_traces_real.jsonl`.

---

## 0. Clock convention (it changed, and that is the point)

`t = 0` is **the moment the user stops speaking** — the last frame of speech leaving the mic.

Frames are fed at **wall-clock rate** (32 ms per 512-sample frame), the way a browser streams a
microphone. This matters: `SemanticEndpointer` accumulates `silence_ms` from *frame durations*, so
a harness that feeds silence as fast as it can collapses the endpointer's 700 ms pause to ~0 wall
time. The old harness never fed frames at all — it handed a pre-cut clip straight to STT.

Consequence: **VAD gate + endpointing pause are counted for the first time.** They were previously
neither measured nor acknowledged in the "full wait" numbers.

---

## 1. Headline — before vs after, same 4 turn shapes

`Before` = `LATENCY_ANALYSIS.md` (harness path, engine only, no VAD/endpointing, dead code path).
`After` = this capture (live path, everything counted). Both are single samples; see §5 on variance.

| Turn | Shape | STT | **First audio** (abs) | **Full wait** | LLM calls | Searches | Discarded drafts | Cost |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| 1 | trivial `hi` | before 604 ms → **after 1141 ms** | 5437 → **7317 ms** | 6084 → **8620 ms** | 2 → **2** | 0 → **0** | 0 → **0** | $0.00133 → **$0.00131** |
| 2 | memory recall | 745 → **916 ms** | 4642 → **11145 ms** | 5805 → **12948 ms** | 2 → **2** | 0 → **0** | 0 → **0** | $0.00149 → **$0.00145** |
| 3 | live search | 950 → **1417 ms** | 4942 → **7654 ms** | 8574 → **10811 ms** | 2 → **2** | 0 → **0** | 0 → **0** | $0.00091 → **$0.00145** |
| 4 | multi-intent | 1542 → **1116 ms** | 4773 → **6093 ms** | 27624 → **27246 ms** | 10 → **9** | 4 → **6** | 3 → **3** | $0.01146 → **$0.01000** |

**Every turn produced audio and zero swallowed exceptions.** Before the fix, all four produced
*none*.

### What actually changed, and why

- **First audio is 1.3–6.5 s worse than reported.** Not a regression — the old number simply did
  not include the VAD gate, the endpointer's pause, prompt assembly on the live path, the TTS
  websocket handshake, or the per-turn work `VoiceSession` does around generation.
- **Turn 4's total looks *better* (27.2 s vs 27.6 s) while doing *more* work** (6 searches vs 4).
  That is ReAct-loop variance, not an improvement. Do not read it as one.
- **Turn 3 still runs 0 searches.** Confirmed independently — see `docs/NEXT_CORRECTNESS_TASK.md`.
  It is a routing gap (`_is_live_info_query` doesn't know "LTP"), not a fixture problem.

---

## 2. The real waterfall (turn 1, trivial `"hi"` — 7317 ms to first audio)

```
   -399   [vad] speech detected (during speech)
      0   ── USER STOPS SPEAKING ──
      0 → ~700   endpointer accumulates silence (short_pause_ms=700)   ← NEVER COUNTED BEFORE
    700 → 1999   _transcribe()                              1141 ms
                   ├─ vocab.terms_for()  Graphiti/Neo4j       426 ms   ← inside the STT span
                   └─ Grok STT batch round-trip              ~715 ms
   1999   [endpoint] complete_thought=True
   1999 → 2900   prompt_assembly (memory reads)              ~900 ms
   2900 → 3822   tts.open_stream() websocket handshake       ~920 ms   ← NEVER COUNTED BEFORE
   3822 → 5695   LLM context_intent                          1872 ms
   5700 → 6879   LLM response                                1179 ms
   7317   ◄── FIRST AUDIO
   8620   reply complete
```

### Three costs that were completely invisible

1. **TTS websocket handshake: 870–1014 ms, every turn.** Measured directly, three consecutive
   calls: `870 ms / 898 ms / 1014 ms`. `VoiceSession._speak_turn` opens a fresh
   `GrokTTSStream` *before* generation starts, so it sits squarely on the critical path — and it
   is paid again for the open-greeting and again for every background delivery. This is the
   single largest fixed cost nobody knew about.
2. **Endpointing pause: ~700 ms** (`short_pause_ms`), or 2500 ms when the endpointer judges the
   thought incomplete. Pure wall time before anything downstream can start.
3. **`vocab.terms_for()`: 426 ms** of Graphiti/Neo4j, *inside* the STT span (it runs within
   `_transcribe`). Cached per session, so it is a first-turn cost — but the trace attributed it
   to STT.

---

## 3. Turn 2 — the outlier that shows how noisy this is

```
  2773 → 9759   LLM context_intent   6985 ms   ← 54% of the whole turn
  9764 → 10546  LLM response          782 ms
```

The same `context_intent` call took **1872 ms on turn 1 and 6985 ms on turn 2** — same model
(`google/gemini-2.5-flash`), same tier, comparable input (465 vs 475 tokens). A 3.7× spread on a
call that runs on every single turn. Any optimization measured on one sample is measuring noise.

---

## 4. Turn 4 — the multi-intent tail (27.2 s)

```
  3040 → 4591   context_intent    (1551 ms)  in=511
  4599 → 5606   ack               (1007 ms)  in=3505   ← overlaps; first audio at 6093 ms
  4609 → 6662   response #1       (2053 ms)  in=4491   ← DISCARDED (requests a tool)
  9083 → 10246  search_summarize  (1163 ms)
 10267 → 12398  response #2       (2131 ms)  in=4782   cache HIT   ← DISCARDED
 14037 → 14979  search_summarize   (941 ms)
 15002 → 16510  response #3       (1508 ms)  in=5324   ← DISCARDED
 18735 → 19758  search_summarize  (1023 ms)
 19777 → 21407  response #4       (1630 ms)  in=5622
```

- **3 discarded response drafts** (~5.7 s of generation the user never heard) — unchanged.
- **6 web_search calls, all sequential, all with the *same* query** repeated verbatim
  (`"weather in Kathmandu right now and OP crypto trading price"` ×2, then two more variants ×2
  each). The model keeps re-asking because the answer never satisfies it. This is worse than the
  4 the old capture saw.
- Input tokens grow 4491 → 4782 → 5324 → 5622. **1 cache hit in 9 calls.**
- The `ack` is now **1007 ms** (was 3608 ms), because it runs concurrently and the prompt is
  smaller — but it still ships a 3505-token input for a throwaway one-liner.
- First audio (6093 ms) is the *ack*, not the answer. The real answer lands at ~21.4 s.

---

## 5. Honesty notes / limits of this baseline

- **Single sample per turn shape.** Turn 2 proves per-call variance can exceed 5 s. Before/after
  claims on one run are meaningless; use ≥3 repeats and compare distributions.
- **The user's utterance is synthesized with Grok TTS, not a human voice.** STT accuracy and
  duration therefore reflect synthetic speech. `"hi"` transcribes as `"Hi!"`.
- **`context_intent` runs on every voice turn.** The `complexity_hint == "simple"` gate exists only
  in `_resolve_context`, a graph node reachable from `generate()` (the text path). The voice path
  calls `_resolve_note` directly and always pays it. Confirmed in the traces above.
- **No acoustic emotion.** `settings.ser_service_url` is empty, so `prompt.emotion` is always
  `None` and `read_register()` always returns `"neutral"`. The "falls back to text-sentiment"
  claim in three docstrings (`voice/emotion.py`, `adapters/ser/emotion2vec_client.py`,
  `core/reasoning/prosody.py`) **has no implementation**. Production tone is therefore *not*
  dynamic today. Running SER needs the GPU service in `services/ser_service/`.
- **The per-turn judge is off** (`settings.langfuse_eval_enabled = False`), so it contributes no
  latency and no quality signal in production. The "judge/evaluator contention" concern is
  currently moot.

---

## 6. Where the time actually goes on a simple turn

| Stage | ms | Note |
|---|--:|---|
| endpointing pause | ~700 | fixed; config (`endpoint_short_pause_ms`) |
| `vocab.terms_for()` | 426 | Graphiti/Neo4j; first turn only, billed to STT |
| Grok STT (batch) | ~715 | one HTTP round-trip, zero interim partials |
| prompt assembly | ~900 | memory reads |
| **TTS websocket handshake** | **~920** | **on the critical path, every turn** |
| `context_intent` LLM | 1872 (σ large) | fires on every turn incl. `"hi"` |
| `response` LLM (to first token) | 1179 | streamed |
| **total to first audio** | **7317** | |

Roughly **2.5 s of the 7.3 s is fixed overhead nobody had measured** (endpoint pause + TTS
handshake + vocab), and a further ~1.9 s is a `context_intent` call the text path already knows how
to skip.

*Captured 2026-07-09 · 4 real turns through `VoiceSession.converse` ·
`scripts/latency_trace_capture.py` → `docs/latency_traces_real.jsonl`.*
