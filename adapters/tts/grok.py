"""Adapter: streaming TTS via OpenRouter (implements ports.tts.TTS, spec §23).

The spec names Grok Voice via OpenRouter /audio/speech; OpenRouter's live
catalog exposes neither (verified) — its supported speech path is audio-out
chat completions (openai/gpt-audio-mini). The spec's own adaptation clause
covers switching provider; the one-OpenRouter-key constraint holds and the
stream is chunked PCM16 (24 kHz), so barge-in interruption still works by
closing the iterator mid-stream.

Text is chunked at clause/sentence boundaries before synthesis and inline
tags ([sigh], <pause>, ...) are never split across chunks (rule 3).
"""

import base64
import json
import logging
import re
from collections.abc import AsyncIterator

import httpx

from config.settings import Settings
from core.cost import CostEntry, CostLedger, CostMetadata

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24_000  # gpt-audio pcm16 output

# Synthesis chunk budget: big enough for natural prosody within a clause,
# small enough that the first audio arrives fast.
MAX_CHUNK_CHARS = 220

_TAG_PATTERN = re.compile(r"(\[[a-z_ ]+\]|<[a-z_ ]+>)", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|(?<=[,;:])\s+")

_SPEAK_INSTRUCTIONS = (
    "Speak the user's text out loud, verbatim, as natural warm conversation. "
    "Inline markers like [sigh], [laugh], [whisper], <pause>, <slow>, "
    "<emphasis> are DELIVERY directions: perform them (a real sigh, a "
    "whispered span, a beat of silence) — never read them out as words."
)


def chunk_for_synthesis(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Clause/sentence chunking that never splits an inline tag (rule 3)."""
    # Protect tags from the splitter by treating them as opaque tokens.
    pieces = _SENTENCE_SPLIT.split(text.strip())
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not piece:
            continue
        candidate = f"{current} {piece}".strip() if current else piece
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current)
    # A tag straddling a boundary means the splitter broke inside it — the
    # tag regex can't match a split tag, so verify every tag survived intact.
    original_tags = _TAG_PATTERN.findall(text)
    chunked_tags = [tag for chunk in chunks for tag in _TAG_PATTERN.findall(chunk)]
    if original_tags != chunked_tags:
        return [text.strip()]  # fall back to one chunk rather than split a tag
    return chunks


class OpenRouterTTS:
    def __init__(self, settings: Settings, ledger: CostLedger | None = None) -> None:
        self._settings = settings
        self._ledger = ledger

    async def speak(
        self,
        text_with_tags: str,
        voice: str | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        total_cost = 0.0
        try:
            for chunk in chunk_for_synthesis(text_with_tags):
                async for audio, cost in self._synthesize(chunk, voice):
                    total_cost += cost
                    if audio:
                        yield audio
        finally:
            # Logged even when barge-in closes the stream early — the spent
            # characters/cost are real either way.
            self._log_cost(user_id, session_id, len(text_with_tags), total_cost)

    async def _synthesize(
        self, text: str, voice: str | None
    ) -> AsyncIterator[tuple[bytes, float]]:
        payload = {
            "model": self._settings.tts_model,
            "modalities": ["text", "audio"],
            "audio": {"voice": voice or self._settings.tts_voice, "format": "pcm16"},
            "stream": True,
            "usage": {"include": True},
            "messages": [
                {"role": "system", "content": _SPEAK_INSTRUCTIONS},
                {"role": "user", "content": text},
            ],
        }
        headers = {"Authorization": f"Bearer {self._settings.open_router_api_key}"}
        async with (
            httpx.AsyncClient(timeout=self._settings.llm_timeout_s) as client,
            client.stream(
                "POST",
                f"{self._settings.open_router_base_url}/chat/completions",
                headers=headers,
                json=payload,
            ) as response,
        ):
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    cost = float((event.get("usage") or {}).get("cost") or 0.0)
                    delta = (event.get("choices") or [{}])[0].get("delta", {})
                    data = (delta.get("audio") or {}).get("data")
                    audio = base64.b64decode(data) if data else b""
                    if audio or cost:
                        yield audio, cost

    def _log_cost(
        self, user_id: str, session_id: str | None, characters: int, cost: float
    ) -> None:
        if self._ledger is None:
            return
        self._ledger.log(
            CostEntry(
                user_id=user_id,
                component="tts",
                provider="openrouter",
                units={"characters": characters},
                cost_usd=cost,
                metadata=CostMetadata(session_id=session_id),
            )
        )
