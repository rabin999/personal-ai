"""Drive a REAL voice turn through the LIVE entrypoint (`VoiceSession.converse`).

This is the path `api/routes/voice.py::voice_ws` runs: real Silero VAD → real semantic
endpointing → real Grok STT → the wired Orchestrator → real Grok TTS. It is deliberately
NOT `orchestrator.generate_spoken(...)` — calling that directly is what let a live-path
TypeError survive a full latency capture (docs/CODE_FLOW.md §0).

Speech is synthesized with the real Grok TTS (24 kHz), resampled to 16 kHz, and fed as
exact 512-sample VAD frames followed by trailing silence so the endpointer commits the turn.

Every exception reaching the turn path is captured WITH its traceback, so "what does the
live app actually do right now" is answered with evidence, not inference.

Run:  PYTHONPATH=. uv run python -m scripts.voice_live_probe --text "hi"
      PYTHONPATH=. uv run python -m scripts.voice_live_probe --greet
"""

from __future__ import annotations

import argparse
import asyncio
import audioop  # PCM resample 24k(TTS) -> 16k(VAD/STT); stdlib
import logging
import traceback
import uuid
from typing import Any

from api.composition import build_pipeline
from config.settings import get_settings
from core.profile.models import AudioPrefs
from voice.emotion import LaggingEmotionProvider
from voice.endpointing import SemanticEndpointer
from voice.pipeline import PipelineConfig
from voice.session import VoiceSession
from voice.sound import LaggingSoundProvider
from voice.trace import TraceEmitter

USER = "u_demo_001"
VAD_FRAME_BYTES = 512 * 2  # Silero @16k requires exactly 512 samples/call
SILENCE_FRAME = b"\x00" * VAD_FRAME_BYTES


class _CaptureHandler(logging.Handler):
    """Capture every log record that carries exc_info, with the real traceback."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.caught: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.exc_info:
            exc_type, exc, tb = record.exc_info
            self.caught.append(
                {
                    "logger": record.name,
                    "message": record.getMessage(),
                    "type": exc_type.__name__ if exc_type else "?",
                    "exc": str(exc),
                    "traceback": "".join(traceback.format_exception(exc_type, exc, tb)),
                }
            )


async def _synth_16k(tts: Any, text: str) -> bytes:
    out = bytearray()
    async for chunk in tts.speak(text, None, user_id=USER, session_id="s_probe_synth"):
        out += chunk
    pcm16, _ = audioop.ratecv(bytes(out), 2, 1, 24_000, 16_000, None)
    return pcm16


async def _frames(pcm: bytes, trailing_silence_frames: int) -> Any:
    """Exact VAD frames: a little lead-in silence, the speech, then trailing silence."""
    for _ in range(5):
        yield SILENCE_FRAME
        await asyncio.sleep(0)
    for i in range(0, len(pcm) - VAD_FRAME_BYTES, VAD_FRAME_BYTES):
        yield pcm[i : i + VAD_FRAME_BYTES]
        await asyncio.sleep(0)
    for _ in range(trailing_silence_frames):
        yield SILENCE_FRAME
        await asyncio.sleep(0)


def _build_session(
    pipeline: Any, session_id: str, trace: TraceEmitter, greet: bool
) -> VoiceSession:
    """Constructed exactly as api/routes/voice.py::_start does."""
    from adapters.vad.silero import SileroVAD

    prefs = AudioPrefs()
    return VoiceSession(
        user_id=USER,
        session_id=session_id,
        vad=SileroVAD(),
        config=PipelineConfig.from_prefs(prefs),
        stt=pipeline.stt,
        endpointer=SemanticEndpointer(
            short_pause_ms=prefs.endpoint_short_pause_ms,
            long_pause_ms=prefs.endpoint_long_pause_ms,
        ),
        assembler=pipeline.assembler,
        generator=pipeline.orchestrator,  # the WIRED engine, like production
        tts=pipeline.tts,
        working=pipeline.working,
        trace=trace,
        episodic=pipeline.episodic,
        emotion=LaggingEmotionProvider(pipeline.ser),
        sound=LaggingSoundProvider(pipeline.sound_classifier),
        dispatcher=pipeline.dispatcher,
        delivery=pipeline.delivery,
        vocab=pipeline.vocab,
        conversations=pipeline.conversations,
        extractor=pipeline.extractor,
        defer_routing=pipeline.settings.defer_memory_routing,
        compactor=pipeline.compactor,
        logs=pipeline.logs,
        evaluator=pipeline.evaluator,
        greet_on_open=greet,
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="hi")
    ap.add_argument("--greet", action="store_true", help="probe the open-greeting path only")
    ap.add_argument("--silence-frames", type=int, default=120)  # ~3.8s > long_pause
    args = ap.parse_args()

    cap = _CaptureHandler()
    logging.getLogger().addHandler(cap)
    logging.getLogger().setLevel(logging.INFO)

    pipeline = await build_pipeline(get_settings())
    events: list[dict[str, Any]] = []
    # Unique per run: the trace store is queried by (user, session), so a fixed id
    # would accumulate spans across probe runs and make one turn look like several.
    session_id = f"s_live_probe_{uuid.uuid4().hex[:8]}"
    trace = TraceEmitter(session_id)

    session = _build_session(pipeline, session_id, trace, greet=args.greet)

    audio_chunks = 0
    audio_bytes = 0

    if args.greet:
        frames = _frames(b"", 3)  # no speech; just let the greeting run
    else:
        speech = await _synth_16k(pipeline.tts, args.text)
        print(f"synthesized {len(speech)} bytes of 16k PCM for {args.text!r}")
        frames = _frames(speech, args.silence_frames)

    async def collect() -> None:
        nonlocal audio_chunks, audio_bytes
        async for chunk in session.converse(frames):
            audio_chunks += 1
            audio_bytes += len(chunk)

    try:
        await asyncio.wait_for(collect(), timeout=120)
    except TimeoutError:
        print("!! probe timed out waiting for the conversation stream to end")

    # TraceEmitter fans out over an asyncio.Queue; drain whatever the session recorded.
    while not trace._queue.empty():
        ev = trace._queue.get_nowait()
        if ev is not None:
            events.append(ev.model_dump())

    print("\n" + "=" * 78)
    print(f"AUDIO OUT: {audio_chunks} chunk(s), {audio_bytes} bytes")
    print("=" * 78)
    if events:
        print("\nTRACE STAGES:")
        for e in events:
            stage = e.get("stage") if isinstance(e, dict) else getattr(e, "stage", "?")
            msg = e.get("message") if isinstance(e, dict) else getattr(e, "message", "")
            print(f"  [{stage}] {str(msg)[:110]}")

    # F2 proof: the turn must genuinely run THROUGH the wired orchestrator. Its graph
    # nodes + per-LLM-call spans go to the durable trace store (not the TraceEmitter).
    spans = await pipeline.traces.traces_for(USER, session_id)
    nodes = [s["data"].get("node") for s in spans if s.get("message") == "graph.node"]
    llm = [
        (s["data"].get("purpose"), s["data"].get("model"), s["data"].get("latency_ms"))
        for s in spans
        if s.get("stage") == "llm"
    ]
    print(f"\nORCHESTRATOR GRAPH NODES: {nodes or '(none — did NOT run through the graph)'}")
    print("LLM CALLS (purpose, model, ms):")
    for p, m, ms in llm:
        print(f"  {p:16} {m:34} {ms}")

    print(f"\nEXCEPTIONS REACHING A LOG HANDLER: {len(cap.caught)}")
    for c in cap.caught:
        print("\n" + "-" * 78)
        print(f"logger={c['logger']}  swallowed-as={c['message']!r}")
        print(f"{c['type']}: {c['exc']}")
        print(c["traceback"])

    working = [t.text for t in pipeline.working.recent(session_id, n=5)]
    print(f"\nWORKING MEMORY after probe: {working}")


if __name__ == "__main__":
    asyncio.run(main())
