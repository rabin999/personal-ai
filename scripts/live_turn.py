"""Drive one real turn through the LIVE voice entrypoint — the shared harness (F4).

Everything that claims to measure or judge the companion must go through
``VoiceSession.converse``, the same code ``api/routes/voice.py::voice_ws`` runs. Calling
``orchestrator.generate_spoken(...)`` directly is what let a live-path ``TypeError`` sit
green through a full 4-turn latency capture and a "judge 1.0 PASS" golden set
(docs/CODE_FLOW.md §0).

Real Silero VAD → real semantic endpointing → real STT → the wired Orchestrator → real TTS.
The user's utterance is synthesized with the real Grok TTS and fed as exact 512-sample VAD
frames, so VAD/endpointing/STT are all exercised and their latency is counted.

The ONLY substitution is the SER port: acoustic emotion needs a GPU service that is not
running (`settings.ser_service_url` is empty), so a scenario that must exercise a specific
emotional read injects a fixed `EmotionRead` through the same port the real provider
implements. That is a port substitution, not an entrypoint bypass.
"""

from __future__ import annotations

import asyncio
import audioop  # PCM resample 24k(TTS) -> 16k(VAD/STT); stdlib
import logging
import time
import traceback
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from core.profile.models import AudioPrefs
from ports.ser import EmotionRead
from voice.endpointing import SemanticEndpointer
from voice.pipeline import PipelineConfig
from voice.session import VoiceSession
from voice.sound import LaggingSoundProvider
from voice.trace import TraceEmitter

logger = logging.getLogger(__name__)

VAD_FRAME_BYTES = 512 * 2  # Silero @16k requires exactly 512 samples/call
SILENCE_FRAME = b"\x00" * VAD_FRAME_BYTES
TTS_SAMPLE_RATE = 24_000
STT_SAMPLE_RATE = 16_000
# ~3.8 s of trailing silence: comfortably past the endpointer's long_pause (2500 ms) so a
# turn always commits, whichever branch the semantic endpointer takes.
DEFAULT_TRAILING_SILENCE_FRAMES = 120


class FixedEmotionProvider:
    """The `LaggingEmotionProvider` surface, pinned to one read (see module docstring).

    `VoiceSession` only ever calls `.current()` and `.schedule()`. Passing None here gives
    exactly what production gives today: no acoustic read at all.
    """

    def __init__(self, read: dict[str, Any] | None) -> None:
        self._read = EmotionRead.model_validate(read) if read else None

    def current(self) -> EmotionRead | None:
        return self._read

    def schedule(self, audio_window: bytes, **_kw: Any) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _CaptureHandler(logging.Handler):
    """Every log record carrying exc_info, with its real traceback."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.caught: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.exc_info:
            exc_type, exc, tb = record.exc_info
            self.caught.append(
                {
                    "logger": record.name,
                    "swallowed_as": record.getMessage(),
                    "type": exc_type.__name__ if exc_type else "?",
                    "exc": str(exc),
                    "traceback": "".join(traceback.format_exception(exc_type, exc, tb)),
                }
            )


@dataclass
class TurnCapture:
    """One real turn, measured from the moment the user stops speaking."""

    utterance: str
    transcript: str = ""
    reply_text: str = ""
    voice_text: str = ""
    action: str = ""
    style_flags: list[str] = field(default_factory=list)
    audio_chunks: int = 0
    audio_bytes: int = 0
    # All relative to END OF SPEECH — the real perceived wait. This INCLUDES the
    # endpointer's pause, which the old harness never counted.
    first_audio_ms: float | None = None
    total_ms: float = 0.0
    stt_ms: float | None = None
    # Wall clock (time.time) at end-of-speech, so trace `ts` and LLM span start/end can be
    # rebased onto the same t0 as first_audio_ms.
    wall_speech_end: float = 0.0
    trace_events: list[Any] = field(default_factory=list)
    spans: list[dict[str, Any]] = field(default_factory=list)
    exceptions: list[dict[str, str]] = field(default_factory=list)
    session_id: str = ""

    # ── derived views over the durable spans ──
    @property
    def llm_calls(self) -> list[dict[str, Any]]:
        return [s["data"] for s in self.spans if s.get("stage") == "llm"]

    @property
    def purposes(self) -> list[str]:
        return [str(c.get("purpose")) for c in self.llm_calls]

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return [s["data"] for s in self.spans if s.get("stage") == "tool"]

    @property
    def searches(self) -> list[str]:
        """DISTINCT search queries actually issued this turn.

        The tool stage emits more than one span per search (a `phase=request` span from
        the dispatcher, plus a `mode=capability_repair` span from the backstop), so a naive
        span count double-reports. Dedupe on the query itself.
        """
        seen: list[str] = []
        for t in self.tool_calls:
            if t.get("tool") != "web_search":
                continue
            q = str((t.get("args") or {}).get("query") or "").strip()
            if q and q not in seen:
                seen.append(q)
        return seen

    @property
    def graph_nodes(self) -> list[str]:
        return [str(s["data"].get("node")) for s in self.spans if s.get("message") == "graph.node"]

    @property
    def ran_context_intent(self) -> bool:
        return "context_intent" in self.purposes

    @property
    def discarded_drafts(self) -> int:
        return max(0, self.purposes.count("response") - 1)

    @property
    def cache_hits(self) -> int:
        return sum(1 for c in self.llm_calls if c.get("cache_hit"))

    @property
    def cost_usd(self) -> float:
        return round(sum(float(c.get("cost_usd") or 0.0) for c in self.llm_calls), 6)


async def synth_16k(tts: Any, text: str, user_id: str) -> bytes:
    """The user's utterance as real speech, resampled to the pipeline's 16 kHz."""
    out = bytearray()
    async for chunk in tts.speak(text, None, user_id=user_id, session_id="s_synth"):
        out += chunk
    pcm16, _ = audioop.ratecv(bytes(out), 2, 1, TTS_SAMPLE_RATE, STT_SAMPLE_RATE, None)
    return pcm16


def build_session(
    pipeline: Any,
    user_id: str,
    session_id: str,
    trace: TraceEmitter,
    *,
    emotion: dict[str, Any] | None = None,
    greet: bool = False,
    barge_in: bool = True,
) -> VoiceSession:
    """Constructed exactly as `api/routes/voice.py::_start` does."""
    from adapters.vad.silero import SileroVAD

    prefs = AudioPrefs()
    return VoiceSession(
        user_id=user_id,
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
        emotion=FixedEmotionProvider(emotion),  # type: ignore[arg-type]
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
        barge_in=barge_in,
        greet_on_open=greet,
    )


async def drive_turn(
    pipeline: Any,
    user_id: str,
    utterance: str,
    *,
    emotion: dict[str, Any] | None = None,
    history: list[tuple[str, str]] | None = None,
    session_id: str | None = None,
    timeout_s: float = 180.0,
    realtime: bool = False,
) -> TurnCapture:
    """Speak ``utterance`` at the companion through the real live path; capture everything.

    The clock starts when the last speech frame is fed — i.e. the instant the user stops
    talking — so `first_audio_ms` is the true perceived wait, VAD + endpointing included.

    ``realtime`` paces the frame feed at wall-clock speed (32 ms per 512-sample frame), the
    way a browser streams microphone audio. This MATTERS: the endpointer accumulates
    ``silence_ms`` from FRAME durations, so feeding silence as fast as possible collapses its
    700 ms pause to ~0 wall time and understates the real perceived latency. Use realtime for
    any number that gets reported as latency; leave it off for functional/quality runs where
    the pause is dead time that only slows the suite down.
    """
    from core.memory.working import Turn

    sid = session_id or f"s_live_{uuid.uuid4().hex[:8]}"
    cap = _CaptureHandler()
    logging.getLogger().addHandler(cap)
    trace = TraceEmitter(sid)
    try:
        for role, text in history or []:
            pipeline.working.append(sid, Turn(role=role, text=text))

        session = build_session(pipeline, user_id, sid, trace, emotion=emotion)
        speech = await synth_16k(pipeline.tts, utterance, user_id)

        speech_end = asyncio.get_running_loop().create_future()
        wall_end: list[float] = []

        # One 512-sample frame is 32 ms of audio at 16 kHz.
        tick = (VAD_FRAME_BYTES / 2) / STT_SAMPLE_RATE if realtime else 0

        async def frames() -> AsyncIterator[bytes]:
            for _ in range(5):  # lead-in silence so the VAD gate sees an onset
                yield SILENCE_FRAME
                await asyncio.sleep(tick)
            for i in range(0, len(speech) - VAD_FRAME_BYTES, VAD_FRAME_BYTES):
                yield speech[i : i + VAD_FRAME_BYTES]
                await asyncio.sleep(tick)
            if not speech_end.done():  # the user has now stopped talking → t0
                wall_end.append(time.time())
                speech_end.set_result(time.perf_counter())
            for _ in range(DEFAULT_TRAILING_SILENCE_FRAMES):
                yield SILENCE_FRAME
                await asyncio.sleep(tick)

        result = TurnCapture(utterance=utterance, session_id=sid)

        async def collect() -> None:
            async for chunk in session.converse(frames()):
                if result.first_audio_ms is None and speech_end.done():
                    result.first_audio_ms = (time.perf_counter() - speech_end.result()) * 1000
                result.audio_chunks += 1
                result.audio_bytes += len(chunk)

        await asyncio.wait_for(collect(), timeout=timeout_s)
        t0 = speech_end.result() if speech_end.done() else time.perf_counter()
        result.total_ms = (time.perf_counter() - t0) * 1000
        result.wall_speech_end = wall_end[0] if wall_end else time.time()
    finally:
        logging.getLogger().removeHandler(cap)

    while not trace._queue.empty():
        ev = trace._queue.get_nowait()
        if ev is not None:
            result.trace_events.append(ev)

    for ev in result.trace_events:
        if ev.stage == "stt" and ev.data.get("duration_ms") is not None:
            result.stt_ms = float(ev.data["duration_ms"])
        elif ev.stage == "stt" and ev.message.startswith("final:"):
            result.transcript = str(ev.data.get("text", ""))
        elif ev.stage == "response" and not ev.data.get("delivered"):
            result.reply_text = ev.message
            result.voice_text = str(ev.data.get("voice_text", ""))
        elif ev.stage == "generation" and ev.data.get("action"):
            result.action = str(ev.data["action"])
        elif ev.stage == "generation" and ev.data.get("style_flags"):
            result.style_flags = list(ev.data["style_flags"])

    result.spans = await pipeline.traces.traces_for(user_id, sid)
    result.exceptions = cap.caught
    result.first_audio_ms = (
        round(result.first_audio_ms, 1) if result.first_audio_ms is not None else None
    )
    result.total_ms = round(result.total_ms, 1)
    return result
