"""Capture REAL turns end-to-end through the LIVE voice path, and export full traces.

**F4/F6 rewrite.** The previous version called
``pipeline.orchestrator.generate_spoken(prompt, dispatcher, ctx, speak)`` — four positional
args, a shape ``VoiceSession`` never uses. That bypassed the live entrypoint entirely, which
is how a ``TypeError`` that silenced every real voice turn produced a clean 4-turn latency
report (docs/CODE_FLOW.md §0, docs/LATENCY_ANALYSIS.md).

It now drives ``VoiceSession.converse`` — real Silero VAD, real semantic endpointing, real
STT, the wired Orchestrator, real TTS — via ``scripts/live_turn.py``. Consequences:

- **VAD + endpointing latency is finally counted.** The clock starts at END OF SPEECH.
- ``first_audio_ms`` is therefore the true perceived wait, not a post-STT proxy.
- The per-turn work the old harness skipped (background-result delivery, vocab boost,
  emotion/sound scheduling, conversation logging, the TTS websocket handshake) is included.

Run:  PYTHONPATH=. uv run python -m scripts.latency_trace_capture
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from api.composition import build_pipeline
from config.settings import get_settings
from scripts.live_turn import TurnCapture, drive_turn

USER = "u_demo_001"
OUT = Path("docs/latency_traces_real.jsonl")

TURNS = [
    ("trivial", "hi"),
    ("memory_recall", "when do I take my meds?"),
    ("live_search", "what's the current LTP of OP?"),
    (
        "multi_intent",
        "what's the weather in Kathmandu, remind me of my dentist at 4, and what's OP trading at?",
    ),
]


def _waterfall(cap: TurnCapture) -> list[dict[str, Any]]:
    """Every stage, rebased onto t0 = end of speech (the moment the user stops talking)."""
    t0 = cap.wall_speech_end
    rows: list[dict[str, Any]] = []
    for ev in cap.trace_events:
        rows.append(
            {
                "kind": "stage",
                "stage": ev.stage,
                "message": ev.message[:90],
                "at_ms": round((ev.ts - t0) * 1000, 1),
                "duration_ms": ev.data.get("duration_ms"),
            }
        )
    for call in cap.llm_calls:
        start, end = call.get("start_ts"), call.get("end_ts")
        rows.append(
            {
                "kind": "llm",
                "purpose": call.get("purpose"),
                "model": call.get("model"),
                "start_ms": round((float(start) - t0) * 1000, 1) if start else None,
                "end_ms": round((float(end) - t0) * 1000, 1) if end else None,
                "duration_ms": call.get("latency_ms"),
                "input_tokens": call.get("input_tokens"),
                "output_tokens": call.get("output_tokens"),
                "cached_tokens": call.get("cached_tokens"),
                "cache_hit": call.get("cache_hit"),
                "cost_usd": call.get("cost_usd"),
            }
        )
    return sorted(rows, key=lambda r: r.get("at_ms") or r.get("start_ms") or 0.0)


def _record(shape: str, idx: int, cap: TurnCapture) -> dict[str, Any]:
    return {
        "turn": idx,
        "shape": shape,
        "prompt": cap.utterance,
        "transcript": cap.transcript,
        "reply": cap.reply_text,
        "action": cap.action,
        "style_flags": cap.style_flags,
        "entrypoint": "VoiceSession.converse (the live path)",
        "totals": {
            # t0 = END OF SPEECH. Includes VAD + endpointing, which the old harness omitted.
            "first_audio_ms": cap.first_audio_ms,
            "total_ms": cap.total_ms,
            "stt_ms": cap.stt_ms,
            "llm_calls": len(cap.llm_calls),
            "searches": len(cap.searches),
            "discarded_drafts": cap.discarded_drafts,
            "cache_hits": cap.cache_hits,
            "cost_usd": cap.cost_usd,
            "audio_chunks": cap.audio_chunks,
            "audio_bytes": cap.audio_bytes,
        },
        "graph_nodes": cap.graph_nodes,
        "llm_purposes": cap.purposes,
        "searches": [t.get("args") for t in cap.searches],
        "exceptions": cap.exceptions,
        "waterfall": _waterfall(cap),
    }


async def main() -> None:
    pipeline = await build_pipeline(get_settings())
    records = []
    for i, (shape, text) in enumerate(TURNS, start=1):
        print(f"--- turn {i}: {shape} :: {text!r}")
        cap = await drive_turn(pipeline, USER, text)
        rec = _record(shape, i, cap)
        records.append(rec)
        t = rec["totals"]
        print(
            f"    first_audio={t['first_audio_ms']}ms  total={t['total_ms']}ms  "
            f"stt={t['stt_ms']}ms  llm={t['llm_calls']}  searches={t['searches']}  "
            f"drafts_discarded={t['discarded_drafts']}  cost=${t['cost_usd']}  "
            f"exc={len(cap.exceptions)}"
        )
        print(f"    reply: {cap.reply_text[:100]!r}")
    OUT.write_text("\n".join(json.dumps(r) for r in records) + "\n")  # noqa: ASYNC240
    print(f"\nwrote {len(records)} turns -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
