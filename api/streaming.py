"""WebSocket/SSE streaming helpers for the serving edge (spec §0.6).

The voice turn produces two interleaved output streams — trace events (JSON,
for the log sidebar) and TTS audio (binary) — that must arrive over one
WebSocket in order. ``merge_conversation`` fans them into one ordered iterator so
the route has exactly one sender (no concurrent-write races). ``reframe``
slices the browser's audio into the fixed frame size the VAD expects.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from voice.session import VoiceSession
from voice.trace import TraceEmitter

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
) -> AsyncIterator[OutItem]:
    """Yield trace events and TTS audio for a whole conversation as one ordered
    stream. Audio chunks are tagged with the current turn so the UI can group
    and replay each reply's audio."""
    out: asyncio.Queue[OutItem | None] = asyncio.Queue()
    live = 2  # trace producer + audio producer

    async def forward_trace() -> None:
        async for event in trace.events():
            await out.put(("json", {"type": "trace", **event.model_dump()}))
        await out.put(None)

    async def drive_audio() -> None:
        try:
            async for chunk in session.converse(frames):
                await out.put(("bytes", chunk))
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
