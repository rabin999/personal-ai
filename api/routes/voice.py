"""Voice WebSocket route (spec §0.6): a continuous spoken conversation + trace.

One WebSocket carries a whole conversation. The client authenticates with its
bearer token (browsers can't set WS auth headers, so it arrives in the first
message — spec §26), then sends ``start_conversation`` and streams PCM16/16kHz
frames continuously. The server takes turns on its own — §19 VAD gate, §21
endpointing, §24 barge-in — and streams back trace events (JSON) + Grok TTS
audio (binary). ``stop_conversation`` ends it.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.composition import Pipeline
from api.streaming import merge_conversation
from core.profile.models import AudioPrefs
from core.psych.consolidation import CONSOLIDATION_TASK_TYPE
from ports.user_context import UserRecord
from voice.emotion import LaggingEmotionProvider
from voice.endpointing import SemanticEndpointer
from voice.pipeline import PipelineConfig, VADModel
from voice.session import TTS_SAMPLE_RATE, VoiceSession
from voice.sound import LaggingSoundProvider
from voice.trace import TraceEmitter

logger = logging.getLogger(__name__)

router = APIRouter()

SAMPLE_RATE = 16_000
VAD_FRAME_BYTES = 512 * 2  # Silero @16kHz requires exactly 512 samples per call


def _build_vad() -> VADModel:
    # A fresh instance per WebSocket: Silero is a stateful RNN, so each session
    # needs its own hidden state — sharing one across concurrent users would mix
    # their audio state. (The repeated "Loading Silero VAD" DEBUG log is quieted
    # by lowering pipecat's log level at startup, not by sharing state.)
    from adapters.vad.silero import SileroVAD  # voice extra (§19)

    return SileroVAD()


def _pipeline_config(user: UserRecord) -> PipelineConfig:
    try:
        return PipelineConfig.from_prefs(AudioPrefs.model_validate(user.audio_prefs))
    except Exception:  # malformed prefs → safe defaults
        return PipelineConfig()


def _endpointer(user: UserRecord) -> SemanticEndpointer:
    prefs = AudioPrefs.model_validate(user.audio_prefs) if user.audio_prefs else AudioPrefs()
    # Keep turn-taking IN SYNC with the companion's speaking pace (user request): a
    # faster voice_speed → proportionally shorter endpoint pauses, so a snappy fast
    # companion also takes its turn snappily, and a slower one is more patient. Scale
    # inversely by voice_speed, clamped so it never gets so short it cuts people off.
    scale = max(0.7, min(1.3, 1.0 / max(0.8, prefs.voice_speed)))
    return SemanticEndpointer(
        short_pause_ms=int(prefs.endpoint_short_pause_ms * scale),
        long_pause_ms=int(prefs.endpoint_long_pause_ms * scale),
    )


class _Conversation:
    """Streams the current conversation's frames in and trace+audio out."""

    def __init__(
        self,
        ws: WebSocket,
        session: VoiceSession,
        trace: TraceEmitter,
        on_event: Callable[[dict[str, object]], Awaitable[None]] | None = None,
        on_end: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._ws = ws
        self.session = session
        self._trace = trace
        self._on_event = on_event
        self._on_end = on_end
        self._frames: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._buffer = bytearray()
        self.task = asyncio.create_task(self._run())

    def feed(self, chunk: bytes) -> None:
        """Reframe incoming audio into exact VAD frames and enqueue."""
        self._buffer.extend(chunk)
        while len(self._buffer) >= VAD_FRAME_BYTES:
            self._frames.put_nowait(bytes(self._buffer[:VAD_FRAME_BYTES]))
            del self._buffer[:VAD_FRAME_BYTES]

    def stop(self) -> None:
        self._frames.put_nowait(None)  # end the continuous frame stream

    async def _frame_iter(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._frames.get()
            if frame is None:
                return
            yield frame

    async def _run(self) -> None:
        try:
            async for kind, payload in merge_conversation(
                self._trace, self.session, self._frame_iter(), self._on_event
            ):
                if kind == "json":
                    await self._ws.send_json(payload)
                else:
                    await self._ws.send_bytes(payload)
            await self._ws.send_json({"type": "conversation_ended"})
        except (WebSocketDisconnect, RuntimeError):
            pass  # client went away mid-conversation
        finally:
            # Session end (explicit stop or disconnect) → post-session learning
            # (§3.6/§18): consolidation runs off the conversation path, in the
            # worker. Best-effort — a failure here never propagates to the socket.
            if self._on_end is not None:
                try:
                    await self._on_end()
                except Exception:
                    logger.exception("session-end consolidation enqueue failed")


def _start(
    ws: WebSocket,
    pipeline: Pipeline,
    user: UserRecord,
    session_id: str,
    vad: VADModel,
    config: PipelineConfig,
    voice: str | None,
) -> _Conversation:
    trace = TraceEmitter(session_id)
    session = VoiceSession(
        user_id=user.user_id,
        session_id=session_id,
        vad=vad,
        config=config,
        stt=pipeline.stt,
        endpointer=_endpointer(user),
        assembler=pipeline.assembler,
        generator=pipeline.orchestrator,
        tts=pipeline.tts,
        working=pipeline.working,
        trace=trace,
        episodic=pipeline.episodic,
        emotion=LaggingEmotionProvider(pipeline.ser),
        sound=LaggingSoundProvider(pipeline.sound_classifier),  # U10-U12
        voice=voice,
        dispatcher=pipeline.dispatcher,
        delivery=pipeline.delivery,
        vocab=pipeline.vocab,
        conversations=pipeline.conversations,
        extractor=pipeline.extractor,
        defer_routing=pipeline.settings.defer_memory_routing,
        compactor=pipeline.compactor,  # F14: rolling-summary compaction
        logs=pipeline.logs,  # C1: bind per-turn so deep LLM-call spans persist
        evaluator=pipeline.evaluator,  # §6/§7: judge voice turns too (Langfuse eval)
    )

    async def persist(event: dict[str, object]) -> None:
        await pipeline.traces.record(user.user_id, event)  # user-scoped (§0.5)

    async def consolidate() -> None:
        """Enqueue post-session consolidation with the whole conversation (§3.6/§18).

        Off the latency path (worker runs it); skipped for an empty session so a
        connect/disconnect with no speech doesn't spawn work."""
        turns = pipeline.working.recent(session_id, n=200)
        if not turns:
            return
        await pipeline.queue.enqueue(
            session_id=session_id,
            user_id=user.user_id,
            type=CONSOLIDATION_TASK_TYPE,
            params={"transcript": [t.model_dump() for t in turns]},
        )

    return _Conversation(ws, session, trace, persist, on_end=consolidate)


async def _session_user(ws: WebSocket) -> UserRecord | None:
    """Resolve the WS handshake's session cookie to a UserRecord (real auth §26).

    Starlette SessionMiddleware populates ``ws.session`` from the signed cookie
    the browser sends on the WS upgrade — so no bearer token in the message.
    """
    user_id = ws.session.get("user_id") if "session" in ws.scope else None
    if not user_id:
        return None
    pipeline: Pipeline = ws.app.state.pipeline
    return await pipeline.user_context.record_for(user_id)


@router.websocket("/ws/voice")
async def voice_ws(ws: WebSocket) -> None:
    await ws.accept()
    pipeline: Pipeline | None = ws.app.state.pipeline
    if pipeline is None:
        await ws.send_json({"type": "error", "message": "pipeline not wired"})
        await ws.close()
        return

    # Identity comes from the signed session cookie the browser sends on the WS
    # handshake (real Google SSO, §26) — no token in the message. The first
    # message still carries the voice selection.
    auth = await ws.receive_json()
    user = await _session_user(ws)
    if user is None:
        await ws.send_json({"type": "error", "message": "unauthorized"})
        await ws.close()
        return

    try:
        vad = _build_vad()
    except ImportError:
        await ws.send_json({"type": "error", "message": "voice extra not installed"})
        await ws.close()
        return

    session_id = f"ws_{uuid.uuid4().hex[:8]}"
    # Pin the voice ONCE for the whole session (spec §2b): normalize the client's
    # choice to a valid id here so every turn uses the same voice — no mid-session
    # change, no silent per-call fallback — and the client + trace see the real id.
    from adapters.tts.grok import resolve_voice

    voice = resolve_voice(auth.get("voice"))
    config = _pipeline_config(user)
    # C7: the user's playback rate (default 1.0x), applied at the client audio sink
    # for BOTH voice engines (it's the shared playback layer behind the voice port).
    try:
        voice_speed = AudioPrefs.model_validate(user.audio_prefs).voice_speed
    except Exception:
        voice_speed = 1.0
    await ws.send_json(
        {
            "type": "ready",
            "session_id": session_id,
            "user_id": user.user_id,
            "companion_name": user.companion_name,
            "sample_rate": TTS_SAMPLE_RATE,
            "voice": voice,  # the pinned voice the client should expect all session
            "voice_speed": voice_speed,  # C7: playback rate for this session
        }
    )

    convo: _Conversation | None = None
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            data = message.get("bytes")
            if text is not None:
                control = json.loads(text)
                kind = control.get("type")
                if kind == "start_conversation":
                    if convo is not None and not convo.task.done():
                        convo.stop()
                    convo = _start(ws, pipeline, user, session_id, vad, config, voice)
                elif kind == "stop_conversation" and convo is not None:
                    convo.stop()
            elif data is not None and convo is not None:
                convo.feed(data)
    except WebSocketDisconnect:
        pass
    finally:
        if convo is not None and not convo.task.done():
            convo.stop()
            convo.task.cancel()
