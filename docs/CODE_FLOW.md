# Code Flow — one voice turn, end to end

Written by reading the code (not the docs) as the mandatory orientation step before the
latency work. Maps `file → function → responsibility → what it calls next`, and marks where
the ports/adapters boundary sits.

**Boundary rule (CLAUDE.md §3.3):** `core/` imports only `ports/`. `adapters/` and `voice/`
may import `core/`. Enforced by `uv run lint-imports`.

---

## 0. The two entrypoints are NOT the same code path

This matters more than anything else in this document.

| Path | Entry | Calls | Used by |
|---|---|---|---|
| **Live voice** | `api/routes/voice.py` `voice_ws` | `VoiceSession.converse` → `_speak_turn` → `generator.generate_spoken(..., temperature=…)` | the real app |
| **Latency harness** | `scripts/latency_trace_capture.py` | `pipeline.orchestrator.generate_spoken(prompt, dispatcher, ctx, speak)` — 4 positional args | `docs/LATENCY_ANALYSIS.md` |

`VoiceSession` is constructed with `generator=pipeline.orchestrator` (`api/routes/voice.py:147`),
i.e. the **`LangGraphOrchestrator`**, but it is *typed* as `ResponseGenerator` and calls it with a
`temperature=` keyword that only `ResponseGenerator.generate_spoken` accepts.

→ **Every live voice turn raises `TypeError`**, swallowed by the broad `except Exception` in
`VoiceSession._run_turn_inner`. The harness bypasses `VoiceSession`, so the latency capture never
saw it. See `docs/LATENCY_ANALYSIS_AFTER.md` §0.

---

## 1. Audio in → transcript

```
browser ──PCM16 16kHz frames──► api/routes/voice.py :: voice_ws
                                    └─► _start(...) builds VoiceSession + _WSFeeder
                                        └─► VoiceSession.converse(frames)         voice/session.py:207
                                            └─► _consume(frames, out)             voice/session.py:234
```

`_consume` is the continuous state machine. Per frame:

| Step | Code | Responsibility |
|---|---|---|
| VAD | `voice/pipeline.py` `AudioInputPipeline.stream` → `VADGate.update` | speech/silence gate (hysteresis). Adapter: `adapters/vad/silero.py` behind no port — `VADModel` Protocol lives in `voice/pipeline.py` |
| barge-in | `voice/session.py:280-315` | if `turn` in flight **or** `time.monotonic() < _playback_until`: 8 sustained frames over `barge_in_threshold` → cancel turn, `_drain(out)`, emit `barge_in` spans |
| pre-roll | `deque(maxlen=10)` | 320 ms of pre-speech audio so the first phoneme isn't clipped |
| endpointing | `voice/endpointing.py` `SemanticEndpointer.decide(transcript, silence_ms)` | short pause (700 ms) if the thought is complete, long pause (2500 ms) otherwise. **Requires a transcript** → STT runs *before* the endpoint decision |
| STT | `VoiceSession._transcribe` → `ports/stt.py::STT.transcribe_stream` | adapter `adapters/stt/grok.py` (**batch**: buffers the whole utterance, one HTTP POST, one final piece, zero partials) or `adapters/stt/faster_whisper.py` (emits partials) |
| multi-utterance | `voice/multiutterance.py` `classify_utterance/combine` | fold a quick addition into the previous turn |

Then: `turn = asyncio.create_task(self._run_turn(transcript, utterance, out))`.

**Note the ordering cost:** `_transcribe` is called *inside* the silence-threshold branch, so STT
latency is paid *after* the pause has already elapsed, and again if the utterance continues.
`speech_since_transcribe` avoids re-transcribing when only silence was added.

---

## 2. Turn → prompt

```
VoiceSession._run_turn                      voice/session.py:446   binds trace_id/turn_id/user_id
  └─► _run_turn_inner                       voice/session.py:463
        ├─ emotion  = self._emotion.current()        voice/emotion.py  LaggingEmotionProvider (ONE TURN BEHIND)
        ├─ sound    = self._sound.current()          voice/sound.py    (one turn behind)
        ├─ _deliver_pending(out)                     §14 background results spoken at the pause
        ├─ working.append(user turn)                 core/memory/working.py
        └─ prompt = assembler.assemble(...)          core/reasoning/prompt_assembly.py:PromptAssembler
```

`PromptAssembler.assemble` is the READ step. It fans out to memory and returns an
`AssembledPrompt` (or a `DisambiguationRequest`):

| Source | Port | Adapter |
|---|---|---|
| working memory | — (in-core) | `core/memory/working.py` |
| episodic | `ports/vector_store.py` | `adapters/vector/qdrant.py` (dense+BM25+RRF) |
| semantic/temporal | `ports/graph_store.py` | `adapters/graph/graphiti.py` (Graphiti + Neo4j) |
| procedural | `ports/doc_store.py` | `adapters/doc/mongo.py` |
| preferences | `ports/preference_memory.py` | `adapters/preference/mem0_adapter.py` |
| reranking | `ports/reranker.py` | `adapters/rerank/fastembed_reranker.py` (off by default) |
| traits/persona | — | `core/profile/`, `core/psych/persona.py` |

Key `AssembledPrompt` fields for this work:
- `complexity_hint` ← `_complexity_hint(utterance)` (**pure length/keyword heuristic**:
  `>60 words or ≥2 heavy markers → complex`; `>12 words or ≥1 marker → moderate`; else `simple`).
- `cache_prefix` ← the rendered **stable** section block, a byte-exact prefix of `system_prompt`.
- `emotion`, `model_override`, `reasoning_model_override`, `suppress_live_search`.

---

## 3. Prompt → reply (the reasoning path)

```
VoiceSession._speak_turn                    voice/session.py:619
  ├─ tts.open_stream()  ──► adapters/tts/grok.py :: GrokTTSStream   (ONE websocket for the whole turn)
  ├─ pump = task(stream.audio() → out)                              PCM chunks → client
  ├─ speak(text) = stream.feed(text)                                text deltas → xAI TTS
  └─ generator.generate_spoken(prompt, dispatcher, context, speak, temperature=…)
        │
        └─► adapters/orchestrator/langgraph_orchestrator.py :: LangGraphOrchestrator.generate_spoken
              ├─ _perceive_span(prompt)                       trace only
              ├─ note, suppress = await _resolve_note(prompt) ◄── LLM call, purpose="context_intent"
              │                                                    ** NO SIMPLE-TURN GATE HERE **
              ├─ turn_prompt = _augment(prompt, note, suppress)
              ├─ result = ResponseGenerator.generate_spoken(turn_prompt, …, speak)
              └─ _reflect_span(...)
```

The `complexity_hint == "simple"` gate exists **only** in `_resolve_context`
(`langgraph_orchestrator.py:179`), a *graph node* reachable only from `generate()` — the **text**
path. The voice path calls `_resolve_note` directly and always pays the call. That is the O1 bug.

### `ResponseGenerator.generate_spoken` (`core/reasoning/response_gen.py:493`)

```
streamable = not pending_confirmation and not _is_live_info_query(utterance)
  │
  ├─ streamable ──► _stream_reply()                      ONE plain-prose streaming LLM call
  │                   └─ per delta: _sentence_end() → _speak_clean() → speak()
  │                      (TTS starts on the first COMPLETE SENTENCE)
  │                   └─ _finish_spoken() → _finish()
  │
  └─ live-info ──► gen_task = task(self.generate(...))    the full agentic path
                   _dynamic_ack(prompt, speak)            CONCURRENT filler, streamed
                   result = await gen_task
                   speak(result.voice_text)
```

### `ResponseGenerator.generate` — the agentic ReAct loop (`response_gen.py:347`)

```
for _ in range(MAX_TOOL_STEPS=4):
    if budget.exceeded(): break                       core/reasoning/response_gen.py:_CostBudget
    turn = await _call_llm(...)                       ◄── LLM, purpose="response", JSON judgment block
    if turn.tool_request is None: break
    note = await _dispatch_tool(...)                  ◄── core/tools/dispatcher.py
    tool_notes.append(note)                           ← the prompt GROWS every step
# capability backstop
if no tool ran and (_is_live_info_query or _needs_capability_repair(draft)):
    _capability_repair()                              ◄── forced web_search + purpose="response_repair"
_finalize(prompt, turn)
    ├─ _curiosity_gate()                              clarify / curious_followup / respond
    ├─ self_model.check_boundary()                    overclaim rewrite
    ├─ _warm_disclosure()                             ◄── LLM (only when requires_nature_disclosure)
    ├─ self-reflection: find_forbidden → _rewrite_assistant_speak() ◄── LLM (only when flagged)
    └─ _finish() → prosody backstop → GenerationResult
```

**The loop re-generates a full `response` draft after every tool result.** On a multi-intent turn
that is 4 drafts, 3 of them discarded (LATENCY_ANALYSIS §5). Tools are dispatched **one at a time**
(`tool_request` is a single object, not a list).

Tool dispatch: `core/tools/dispatcher.py::dispatch` / `run_inline` (8 s inline budget) →
`core/tools/web_search.py::WebSearch.run` → `ports/search.py` → `adapters/search/serper.py`
(fallback `brave.py`), then a **`search_summarize` LLM call** per search.

---

## 4. Reply → audio

`speak(sentence)` (the closure in `_speak_turn`) → `GrokTTSStream.feed()` → xAI websocket →
`audio.delta` frames → `_pump` → `out` queue → `converse` yields → `voice_ws` sends to browser.

`converse` (`voice/session.py:216-227`) accumulates `_playback_until` per chunk so barge-in stays
armed for the whole *client-side* playback, not just while the turn task runs.

Fallback when the websocket won't open: per-sentence REST `GrokTTS.speak` → `chunk_for_synthesis`
(clause/sentence chunks ≤220 chars, never splitting an inline tag) → `POST /v1/tts` streaming body.

---

## 5. After the reply

```
working.append(assistant turn)
_remember(...)          → deferred (defer_memory_routing=True) → the raw log only
_log_conversation(...)  → core/memory/conversation_store.py  (Mongo)  [asyncio task]
_compact_if_needed()    → core/memory/compaction.py                    [asyncio task]
evaluator.schedule(...) → core/eval/evaluator.py TurnEvaluator         [asyncio.create_task]
                          └─ judge_companion_voice (core/eval/judge.py) ◄── separate judge LLM call
                          ** currently DISABLED: settings.langfuse_eval_enabled = False **
```

Memory routing/extraction runs in `workers/consolidation_worker.py` off a raw-log cursor — not on
the turn. Inline memory write measured at 2–8 ms.

---

## 6. Tracing

Every stage calls `TraceEmitter.emit` (`voice/trace.py`) or `StructuredLogger.log`
(`core/observability/logger.py`). The LLM adapter (`adapters/llm/openrouter.py::_log_call`) emits a
per-call span with `purpose / model / tier / tokens / cost_usd / latency_ms / start_ts / end_ts /
cache_hit`. Spans are correlation-bound to `(trace_id=session_id, turn_id)` by
`logs.bind(...)` in `_run_turn`. Sinks: `adapters/logging/` (file, stdout, trace_store) and
`adapters/tracing/langfuse_sink.py`.

**What is NOT instrumented** (LATENCY_ANALYSIS §9, the O10 list): token-level TTFT, per-stage
start/end for STT/assembly/VAD/endpointing/SER/TTS, VAD+endpointing decision latency, SER inference
latency, the judge call, the web_search HTTP-vs-cache-vs-summarize split, and *why* a prompt cache
missed.

---

## 7. Where the boundary sits

- `core/reasoning/orchestrator.py` defines the `Orchestrator` **Protocol** and lives in `core/`
  (not `ports/`) because it returns `GenerationResult`, a core domain type; `ports/` may not import
  `core`. LangGraph is imported only inside `adapters/orchestrator/`.
- `core/reasoning/response_gen.py` depends on `ports/llm.py`, `ports/prompt.py` and core types only.
- `voice/` is a serving edge: it may import `core/` and `ports/`, and is handed adapters by
  `api/composition.py::build_pipeline`.

Consequence for this work: any new port (e.g. a streaming-STT capability, a TTS first-chunk clock)
is declared in `ports/`, implemented in `adapters/`, and consumed in `core/` or `voice/` — never the
other way round.
