"""WebSocket/SSE streaming helpers for the serving edge (spec §0.6).

The voice turn produces two interleaved output streams — trace events (JSON,
for the log sidebar) and TTS audio (binary) — that must arrive over one
WebSocket in order. ``merge_conversation`` fans them into one ordered iterator so
the route has exactly one sender (no concurrent-write races). ``reframe``
slices the browser's audio into the fixed frame size the VAD expects.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from core.errors import PROGRAMMING_ERRORS
from voice.session import VoiceSession
from voice.trace import TraceEmitter

logger = logging.getLogger(__name__)

# (kind, payload): "json" → send_json, "bytes" → send_bytes.
OutItem = tuple[str, Any]

TTS_SAMPLE_RATE = 24_000


async def reframe(source: AsyncIterator[bytes], frame_bytes: int) -> AsyncIterator[bytes]:
    """Re-slice a byte stream into exact ``frame_bytes`` frames (VAD needs fixed frames)."""
    buffer = bytearray()
    async for chunk in source:
        buffer.extend(chunk)
        while len(buffer) >= frame_bytes:
            yield bytes(buffer[:frame_bytes])
            del buffer[:frame_bytes]


async def merge_conversation(
    trace: TraceEmitter,
    session: VoiceSession,
    frames: AsyncIterator[bytes],
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> AsyncIterator[OutItem]:
    """Yield trace events and TTS audio for a whole conversation as one ordered
    stream. Audio chunks are tagged with the current turn so the UI can group
    and replay each reply's audio.

    ``on_event`` (optional) receives every trace event for durable persistence
    (brief §1). It is fired-and-forgotten so it never blocks the WS send path.
    """
    out: asyncio.Queue[OutItem | None] = asyncio.Queue()
    live = 2  # trace producer + audio producer
    sink_tasks: set[asyncio.Task[None]] = set()

    async def _persist(payload: dict[str, Any]) -> None:
        assert on_event is not None
        await on_event(payload)

    async def forward_trace() -> None:
        async for event in trace.events():
            payload = event.model_dump()
            if on_event is not None:  # durable persistence, off the send path
                task: asyncio.Task[None] = asyncio.create_task(_persist(payload))
                sink_tasks.add(task)
                task.add_done_callback(sink_tasks.discard)
            await out.put(("json", {"type": "trace", **payload}))
        await out.put(None)

    async def drive_audio() -> None:
        try:
            async for chunk in session.converse(frames):
                await out.put(("bytes", chunk))
        except asyncio.CancelledError:
            raise
        except PROGRAMMING_ERRORS:
            # F3: `converse` re-raises our own bugs on purpose. The `gather(...,
            # return_exceptions=True)` below would swallow this task's exception, so log
            # it loudly HERE — a conversation that died on a defect must not look like a
            # conversation that ended normally.
            logger.exception("voice conversation aborted by a PROGRAMMING ERROR")
            raise
        except Exception:
            logger.exception("voice conversation aborted by a dependency failure")
            raise
        finally:
            trace.close()  # ends the trace producer once the conversation is done
            await out.put(None)

    tasks = [asyncio.create_task(forward_trace()), asyncio.create_task(drive_audio())]
    try:
        while live:
            item = await out.get()
            if item is None:
                live -= 1
                continue
            yield item
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Let in-flight persistence writes finish (don't drop the last events).
        if sink_tasks:
            await asyncio.gather(*sink_tasks, return_exceptions=True)
