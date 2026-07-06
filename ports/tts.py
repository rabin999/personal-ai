"""Port: speech synthesis with inline delivery tags (spec §23).

``speak`` yields raw PCM16 audio chunks as they stream; the consumer stops
playback on barge-in by closing the iterator (§24).
"""

from collections.abc import AsyncIterator
from typing import Protocol


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
