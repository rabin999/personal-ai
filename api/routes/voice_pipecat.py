"""Pipecat voice WebSocket route (CLAUDE.md §5) — framework-owned VAD + barge-in.

Parallel to the native ``/ws/voice`` (which stays the default): this endpoint runs
the SAME reasoning engine through a Pipecat pipeline, so the framework handles the
transport, VAD, endpointing, and interruption instead of the hand-rolled loop. The
browser wire protocol is unchanged (raw PCM16 in @16k, out @24k) via the raw-PCM
serializer, so the existing AudioWorklet client can drive it.

The client authenticates in the first WS message (browsers can't set WS headers,
§26), then Pipecat's transport takes over the socket. Verified headlessly at the
component level (STT/TTS/reasoning processors + serializer); the end-to-end audio
round-trip is validated in the browser.
"""

import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ports.user_context import Unauthorized

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/voice-pipecat")
async def voice_pipecat_ws(ws: WebSocket) -> None:
    await ws.accept()
    pipeline = ws.app.state.pipeline
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
        from voice.pipecat.runtime import build_pipeline, build_transport, run_pipeline
    except ImportError:
        await ws.send_json({"type": "error", "message": "voice extra not installed"})
        await ws.close()
        return

    session_id = f"pc_{uuid.uuid4().hex[:8]}"
    await ws.send_json({"type": "ready", "session_id": session_id, "user_id": user.user_id})

    transport = build_transport(ws)
    graph = build_pipeline(
        transport,
        user_id=user.user_id,
        session_id=session_id,
        pipeline=pipeline,
        voice=auth.get("voice"),
    )
    try:
        await run_pipeline(graph)
    except WebSocketDisconnect:
        pass
    except Exception:  # never crash the edge on a voice error
        logger.exception("pipecat voice session failed")
