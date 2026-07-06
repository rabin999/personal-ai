"""Voice WebSocket route (spec §0.6): live mic → companion, with a trace feed.

One WebSocket carries a whole conversation. The client authenticates with its
bearer token (browsers can't set WS auth headers, so it arrives in the first
message — spec §26), then per utterance streams PCM16/16kHz frames between
``start`` and ``stop``. The server runs the turn through the VoiceSession and
streams back trace events (JSON) + TTS audio (binary). A ``start`` arriving
while audio is still playing is a barge-in (§24): the in-flight turn is stopped.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.composition import Pipeline
from api.streaming import merge_turn
from core.profile.models import AudioPrefs
from ports.user_context import Unauthorized, UserRecord
from voice.emotion import LaggingEmotionProvider
from voice.endpointing import SemanticEndpointer
from voice.pipeline import PipelineConfig, VADModel
from voice.session import VoiceSession
from voice.trace import TraceEmitter

logger = logging.getLogger(__name__)

router = APIRouter()

SAMPLE_RATE = 16_000
VAD_FRAME_BYTES = 256 * 2  # Silero @16kHz wants exactly 256 samples per call


def _build_vad() -> VADModel:
    from pipecat.audio.vad.silero import SileroVADAnalyzer  # voice extra (§19)

    return SileroVADAnalyzer(sample_rate=SAMPLE_RATE)


def _pipeline_config(user: UserRecord) -> PipelineConfig:
    try:
        return PipelineConfig.from_prefs(AudioPrefs.model_validate(user.audio_prefs))
    except Exception:  # malformed prefs → safe defaults
        return PipelineConfig()


class _Utterance:
    """Collects the current utterance's frames and drives its turn task."""

    def __init__(self, ws: WebSocket, session: VoiceSession, trace: TraceEmitter) -> None:
        self._ws = ws
        self.session = session
        self._trace = trace
        self._frames: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._buffer = bytearray()
        self.task = asyncio.create_task(self._run())

    def feed(self, chunk: bytes) -> None:
        """Reframe incoming audio into exact VAD frames and enqueue."""
        self._buffer.extend(chunk)
        while len(self._buffer) >= VAD_FRAME_BYTES:
            self._frames.put_nowait(bytes(self._buffer[:VAD_FRAME_BYTES]))
            del self._buffer[:VAD_FRAME_BYTES]

    def end(self) -> None:
        self._frames.put_nowait(None)  # close the utterance stream

    async def _frame_iter(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._frames.get()
            if frame is None:
                return
            yield frame

    async def _run(self) -> None:
        try:
            async for kind, payload in merge_turn(
                self._trace, self.session, self._frame_iter()
            ):
                if kind == "json":
                    await self._ws.send_json(payload)
                else:
                    await self._ws.send_bytes(payload)
            await self._ws.send_json({"type": "turn_end"})
        except (WebSocketDisconnect, RuntimeError):
            pass  # client went away mid-turn


def _start_turn(
    ws: WebSocket, pipeline: Pipeline, user: UserRecord, session_id: str,
    vad: VADModel, config: PipelineConfig, voice: str | None,
) -> _Utterance:
    trace = TraceEmitter(session_id)
    session = VoiceSession(
        user_id=user.user_id,
        session_id=session_id,
        vad=vad,
        config=config,
        stt=pipeline.stt,
        endpointer=SemanticEndpointer(),
        assembler=pipeline.assembler,
        generator=pipeline.generator,
        tts=pipeline.tts,
        working=pipeline.working,
        trace=trace,
        episodic=pipeline.episodic,
        emotion=LaggingEmotionProvider(pipeline.ser),
        voice=voice,
    )
    return _Utterance(ws, session, trace)


@router.websocket("/ws/voice")
async def voice_ws(ws: WebSocket) -> None:
    await ws.accept()
    pipeline: Pipeline | None = ws.app.state.pipeline
    if pipeline is None:
        await ws.send_json({"type": "error", "message": "pipeline not wired"})
        await ws.close()
        return

    auth = await ws.receive_json()
    try:
        user = await pipeline.user_context.resolve(auth.get("token", ""))
    except Unauthorized:
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
    voice = auth.get("voice")
    config = _pipeline_config(user)
    await ws.send_json(
        {"type": "ready", "session_id": session_id, "user_id": user.user_id,
         "companion_name": user.companion_name}
    )

    current: _Utterance | None = None
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
                if kind == "start":
                    if current is not None and not current.task.done():
                        await current.session.on_barge_in()  # §24: talk over playback
                        current.task.cancel()
                    current = _start_turn(ws, pipeline, user, session_id, vad, config, voice)
                elif kind == "stop" and current is not None:
                    current.end()
                elif kind == "barge_in" and current is not None:
                    await current.session.on_barge_in()
            elif data is not None and current is not None:
                current.feed(data)
    except WebSocketDisconnect:
        pass
    finally:
        if current is not None and not current.task.done():
            current.task.cancel()
