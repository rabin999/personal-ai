"""Port: speech synthesis with inline delivery tags (spec §23).

``speak`` yields raw PCM16 audio chunks as they stream; the consumer stops
playback on barge-in by closing the iterator (§24).
"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable


class TTSStream(Protocol):
    """One open synthesis session for a whole turn: feed text as it's generated,
    read audio as it streams. A single session keeps ONE consistent voice across
    every sentence (§2b) — the reason streaming replies don't drift in tone."""

    async def feed(self, text: str) -> None:
        """Push more text to synthesize (a sentence as it's generated)."""
        ...

    async def finish(self) -> None:
        """Signal end-of-text so the tail is flushed."""
        ...

    def audio(self) -> AsyncIterator[bytes]:
        """Yield PCM16 chunks as they stream back."""
        ...

    async def aclose(self) -> None:
        """Stop synthesis (barge-in §24 / turn end) and log cost."""
        ...


@runtime_checkable
class StreamingTTS(Protocol):
    """A TTS adapter that can open a per-turn streaming session (§23). Checked at
    runtime so the voice loop can prefer it and fall back to ``speak`` if absent."""

    async def open_stream(
        self, voice: str | None = None, *, user_id: str, session_id: str | None = None
    ) -> TTSStream: ...


class TTS(Protocol):
    def speak(
        self,
        text_with_tags: str,
        voice: str | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Synthesize tagged text; yields PCM16 audio chunks (streaming)."""
        ...
