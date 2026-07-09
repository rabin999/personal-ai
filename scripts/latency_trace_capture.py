"""Capture 4 REAL turns end-to-end and export full per-turn traces (measurement task).

NOT an optimization. Drives the real reasoning engine (real OpenRouter models, real
Mongo/Qdrant/Neo4j/Redis, real Serper) over four turn shapes, plus the real Grok STT
(batch) and Grok TTS components, and writes one JSON object per turn to
docs/latency_traces.jsonl. Every number is measured; nothing is simulated.

Run:  cd /opt/companion && PYTHONPATH=. uv run python -m scripts.latency_trace_capture
"""

from __future__ import annotations

import asyncio
import audioop  # PCM resample 24k(TTS) -> 16k(STT); stdlib
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from api.composition import build_pipeline
from config.settings import get_settings
from core.memory.working import Turn
from core.reasoning.prompt_assembly import DisambiguationRequest
from core.tools.registry import ToolContext

USER = "u_demo_001"
OUT = Path("docs/latency_traces.jsonl")

TURNS = [
    ("trivial", "hi"),
    ("memory_recall", "when do I take my meds?"),
    ("live_search", "what's the current LTP of OP?"),
    (
        "multi_intent",
        "what's the weather in Kathmandu, remind me of my dentist at 4, and what's OP trading at?",
    ),
]


async def _one_shot(pcm: bytes, chunk: int = 3200) -> AsyncIterator[bytes]:
    for i in range(0, len(pcm), chunk):
        yield pcm[i : i + chunk]


async def _synth(tts: Any, text: str) -> bytes:
    out = bytearray()
    async for c in tts.speak(text, None, user_id=USER, session_id="s_synth"):
        out += c
    return bytes(out)


async def _measure_stt(pipeline: Any, prompt_text: str, sid: str) -> dict[str, Any]:
    """Real STT: synthesize the utterance (Grok TTS 24k) -> resample to 16k -> feed
    the whole VAD-bounded clip to the real STT and measure end-of-audio -> final
    transcript (the batch gap). Reports the ACTUAL transcript (accuracy is a finding)."""
    audio24 = await _synth(pipeline.tts, prompt_text)
    audio16, _ = audioop.ratecv(audio24, 2, 1, 24_000, 16_000, None)
    audio_s = len(audio16) / (16_000 * 2)
    t0 = time.perf_counter()
    final = ""
    partials = 0
    async for piece in pipeline.stt.transcribe_stream(
        _one_shot(audio16), None, user_id=USER, session_id=sid
    ):
        if piece.is_final:
            final = piece.text
        else:
            partials += 1
    gap_ms = (time.perf_counter() - t0) * 1000
    return {
        "engine": pipeline.settings.stt_engine,
        "mode": "batch (VAD-bounded utterance, one HTTP round-trip)"
        if pipeline.settings.stt_engine == "grok"
        else "streaming (interim partials)",
        "audio_s": round(audio_s, 2),
        "gap_ms": round(gap_ms, 1),
        "interim_partials": partials,
        "transcript": final,
    }


async def _run_turn(pipeline: Any, shape: str, prompt_text: str, idx: int) -> dict[str, Any]:
    sid = f"s_lat_{idx}"
    # STT first (the user "speaking" is not part of the post-speech wait; the wait —
    # and thus turn t0 — starts the moment they STOP, i.e. STT begins).
    stt = await _measure_stt(pipeline, prompt_text, sid)

    wall0 = time.time()
    p0 = time.perf_counter()
    marks: list[dict[str, Any]] = []

    def mark(step: str, s: float, e: float, **extra: Any) -> None:
        marks.append(
            {
                "step": step,
                "start_ms": round((s - p0) * 1000, 1),
                "end_ms": round((e - p0) * 1000, 1),
                "duration_ms": round((e - s) * 1000, 1),
                **extra,
            }
        )

    # STT stage occupies [0, gap] of the turn wait.
    marks.append(
        {
            "step": "stt",
            "purpose": "speech->text",
            "start_ms": 0.0,
            "end_ms": stt["gap_ms"],
            "duration_ms": stt["gap_ms"],
            "mode": stt["mode"],
        }
    )

    pipeline.working.append(sid, Turn(role="user", text=prompt_text))

    a0 = time.perf_counter()
    prompt = await pipeline.assembler.assemble(USER, sid, prompt_text)
    a1 = time.perf_counter()
    mark("prompt_assembly", a0, a1, purpose="read memory + build prompt")
    if isinstance(prompt, DisambiguationRequest):
        return {
            "turn": idx,
            "shape": shape,
            "prompt": prompt_text,
            "note": "disambiguation",
            "stt": stt,
            "steps": marks,
        }

    ctx = ToolContext(user_id=USER, session_id=sid, project_id=None)
    first_speak: float | None = None
    first_audio: float | None = None
    sentences = 0
    audio_bytes = 0

    async def speak(sentence: str) -> None:
        nonlocal first_speak, first_audio, sentences, audio_bytes
        if first_speak is None:
            first_speak = time.perf_counter()  # first spoken sentence ready (TTFT proxy)
        sentences += 1
        async for chunk in pipeline.tts.speak(sentence, None, user_id=USER, session_id=sid):
            if first_audio is None:
                first_audio = time.perf_counter()  # first audible chunk
            audio_bytes += len(chunk)

    g0 = time.perf_counter()
    with pipeline.logs.bind(trace_id=sid, turn_id=1, user_id=USER):
        result = await pipeline.orchestrator.generate_spoken(
            prompt, pipeline.dispatcher, ctx, speak
        )
    g1 = time.perf_counter()
    mark(
        "generation+tts",
        g0,
        g1,
        purpose="reason + tools + stream reply",
        action=result.action,
        sentences=sentences,
    )

    # Memory write (inline part only; routing is deferred to the worker).
    m0 = time.perf_counter()
    pipeline.working.append(sid, Turn(role="assistant", text=result.final_text))
    if pipeline.conversations is not None:
        await pipeline.conversations.record_turn(
            user_id=USER,
            session_id=sid,
            turn_index=1,
            user_text=prompt_text,
            assistant_text=result.final_text,
            trace_turn=1,
        )
    m1 = time.perf_counter()
    mark(
        "memory_write_inline",
        m0,
        m1,
        purpose="append working + raw log (routing DEFERRED to worker)",
    )

    total_ms = (time.perf_counter() - p0) * 1000
    # Pull the per-LLM-call + tool + reasoning spans this turn persisted.
    spans = await pipeline.traces.traces_for(USER, sid)
    llm_calls, tool_spans, other = [], [], []
    for s in spans:
        d = s.get("data", {})
        stage = s.get("stage", "")
        rel = round((float(s.get("ts", wall0)) - wall0) * 1000, 1)
        if stage == "llm":
            llm_calls.append(
                {
                    "purpose": d.get("purpose"),
                    "model": d.get("model"),
                    "tier": d.get("tier"),
                    "params": d.get("params"),
                    "input_tokens": d.get("input_tokens"),
                    "output_tokens": d.get("output_tokens"),
                    "cost_usd": d.get("cost_usd"),
                    "latency_ms": d.get("latency_ms"),
                    "cache_hit": d.get("cache_hit"),
                    "cached_tokens": d.get("cached_tokens"),
                    "start_ms": round((float(d.get("start_ts") or wall0) - wall0) * 1000, 1)
                    if d.get("start_ts")
                    else None,
                    "end_ms": round((float(d.get("end_ts") or wall0) - wall0) * 1000, 1)
                    if d.get("end_ts")
                    else None,
                    "completion_preview": str(d.get("completion", ""))[:160],
                }
            )
        elif stage == "tool":
            tool_spans.append(
                {
                    "ts_rel_ms": rel,
                    "message": s.get("message"),
                    "tool": d.get("tool"),
                    "phase": d.get("phase"),
                    "args": d.get("args"),
                    "status": d.get("status"),
                    "result": str(d.get("result", ""))[:200],
                }
            )
        else:
            other.append({"ts_rel_ms": rel, "stage": stage, "message": s.get("message")})

    cost = round(sum((c.get("cost_usd") or 0.0) for c in llm_calls), 6)
    return {
        "turn": idx,
        "shape": shape,
        "prompt": prompt_text,
        "reply_preview": result.final_text[:200],
        "action": result.action,
        "totals": {
            "total_e2e_ms": round(total_ms, 1),
            "stt_gap_ms": stt["gap_ms"],
            "time_to_first_audio_ms": round((first_audio - p0) * 1000, 1) if first_audio else None,
            "time_to_first_sentence_ms": round((first_speak - p0) * 1000, 1)
            if first_speak
            else None,
            "llm_calls": len(llm_calls),
            "tool_call_events": len(tool_spans),
            "cost_usd": cost,
            "audio_bytes": audio_bytes,
        },
        "stt": stt,
        "steps": sorted(marks, key=lambda m: m["start_ms"]),
        "llm_calls": llm_calls,
        "tool_spans": tool_spans,
        "other_spans": other,
    }


async def main() -> None:
    pipeline = await build_pipeline(get_settings())
    records = []
    for i, (shape, text) in enumerate(TURNS, start=1):
        print(f"--- turn {i}: {shape} :: {text!r}")
        rec = await _run_turn(pipeline, shape, text, i)
        records.append(rec)
        t = rec.get("totals", {})
        print(
            f"    e2e={t.get('total_e2e_ms')}ms  stt={t.get('stt_gap_ms')}ms  "
            f"ttfa={t.get('time_to_first_audio_ms')}ms  llm_calls={t.get('llm_calls')}  "
            f"tools={t.get('tool_call_events')}  cost=${t.get('cost_usd')}"
        )
    OUT.write_text("\n".join(json.dumps(r) for r in records) + "\n")  # noqa: ASYNC240 — one-shot script
    print(f"\nwrote {len(records)} turns -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
