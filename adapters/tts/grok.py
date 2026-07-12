"""Adapter: Grok Voice TTS via the xAI TTS API (implements ports.tts.TTS, §23).

The spec's chosen voice. xAI exposes it at ``POST https://api.x.ai/v1/tts``
(not the OpenAI-style /audio/speech): raw PCM16 out, inline delivery tags
(`[laugh] [sigh] [whisper] <emphasis> <slow> <pause>`), five voices
(ara/eve/leo/rex/sal), ~$4.20 / 1M characters.

Text is chunked at clause/sentence boundaries before synthesis and inline
tags are never split across chunks (rule 3); each chunk streams its PCM as it
downloads, so a barge-in (§24) stops playback by closing the iterator between
or mid chunk. Character cost is logged to the Cost Ledger (rule 5).
"""

import base64
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from urllib.parse import urlencode

import httpx
import websockets

from config.settings import Settings
from core.cost import CostEntry, CostLedger, CostMetadata

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24_000  # PCM16 output we request from xAI
_COST_PER_CHAR_USD = 4.20 / 1_000_000  # xAI Grok TTS pricing
# The full xAI Grok voice roster (5 original + 21 flagship, all multilingual — from
# GET /v1/tts/voices). Kept as a static fallback so the app has valid voices even if
# the live catalog fetch fails; ``GrokTTS.list_voices`` fetches names/gender live.
VOICES = (
    "altair", "ara", "atlas", "carina", "castor", "celeste", "cosmo", "eve",
    "helios", "helix", "iris", "kepler", "leo", "lumen", "luna", "lux", "naksh",
    "orion", "perseus", "rex", "rigel", "sal", "sirius", "ursa", "zagan", "zenith",
)  # fmt: skip
_VOICES = set(VOICES)
# Default voice: "helix" — the chosen companion voice for the app. Users can pick any of
# the 26 in the UI; the exact tonal preference is best confirmed by ear (marked in TEST_REPORT).
DEFAULT_VOICE = "helix"


_VOICE_GENDER = {v: "female" for v in ("ara", "carina", "celeste", "eve", "iris", "luna", "ursa")}


def _fallback_voices() -> list[dict[str, str]]:
    """Static roster (id/name/gender) used when the live catalog is unreachable."""
    return [
        {"voice_id": v, "name": v.title(), "gender": _VOICE_GENDER.get(v, "male")} for v in VOICES
    ]


def resolve_voice(voice: str | None, default: str = DEFAULT_VOICE) -> str:
    """Normalize a requested voice to a valid xAI voice id, ONCE (spec §2b).

    Pinning the voice at session start — instead of letting each `speak()` call
    re-derive it and silently fall back — guarantees one consistent voice for the
    whole session (no mid-session voice change) and gives the trace a concrete
    value to record, so a voice change would be visible instead of silent. Always
    returns a valid id even if both the request and the default are bad."""
    for candidate in (voice, default, DEFAULT_VOICE):
        if candidate and candidate.lower() in _VOICES:
            return candidate.lower()
    return DEFAULT_VOICE


# Synthesis chunk budget: big enough for natural prosody within a clause,
# small enough that the first audio arrives fast.
MAX_CHUNK_CHARS = 220

_TAG_PATTERN = re.compile(r"(\[[a-z_ ]+\]|<[a-z_ ]+>)", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|(?<=[,;:])\s+")


def chunk_for_synthesis(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Clause/sentence chunking that never splits an inline tag (rule 3)."""
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
    # A tag straddling a boundary means the splitter broke inside it — the tag
    # regex can't match a split tag, so verify every tag survived intact.
    original_tags = _TAG_PATTERN.findall(text)
    chunked_tags = [tag for chunk in chunks for tag in _TAG_PATTERN.findall(chunk)]
    if original_tags != chunked_tags:
        return [text.strip()]  # fall back to one chunk rather than split a tag
    return chunks


class GrokTTS:
    def __init__(self, settings: Settings, ledger: CostLedger | None = None) -> None:
        self._settings = settings
        self._ledger = ledger
        self._voices_cache: list[dict[str, str]] | None = None

    async def list_voices(self) -> list[dict[str, str]]:
        """The live xAI voice roster (id + name + gender) for the picker (#19). Fetched
        once from GET /v1/tts/voices and cached; falls back to the static roster if the
        catalog is unreachable so the UI always has valid choices."""
        if self._voices_cache is not None:
            return self._voices_cache
        try:
            headers = {"Authorization": f"Bearer {self._settings.xai_api_key}"}
            url = f"{self._settings.xai_base_url}/tts/voices"
            async with httpx.AsyncClient(timeout=self._settings.tts_timeout_s) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            raw = data.get("voices", data) if isinstance(data, dict) else data
            voices = [
                {
                    "voice_id": str(v.get("voice_id") or v.get("id") or "").lower(),
                    "name": str(v.get("name") or v.get("voice_id") or "").title(),
                    "gender": str(v.get("gender") or ""),
                }
                for v in raw
                if v.get("voice_id") or v.get("id")
            ]
            self._voices_cache = voices or _fallback_voices()
        except Exception:
            logger.warning("could not fetch xAI voice catalog; using static roster")
            self._voices_cache = _fallback_voices()
        return self._voices_cache

    async def speak(
        self,
        text_with_tags: str,
        voice: str | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        # The voice is normally already pinned at the session edge (§2b); resolve
        # again here as the single source of truth so a direct caller can't slip an
        # invalid id through, honoring the configured default.
        voice_id = resolve_voice(voice, self._settings.tts_voice)
        spoken_chars = 0
        try:
            for chunk in chunk_for_synthesis(text_with_tags):
                spoken_chars += len(chunk)
                async for audio in self._synthesize(chunk, voice_id):
                    if audio:
                        yield audio
        finally:
            # Logged even when barge-in closes the stream early — the chars
            # submitted to xAI are billed either way (rule 5).
            self._log_cost(user_id, session_id, spoken_chars)

    async def open_stream(
        self,
        voice: str | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> "GrokTTSStream":
        """Open ONE bidirectional WebSocket synthesis session for a whole turn
        (xAI wss /v1/tts, §23). Feeding every sentence of a reply into a single
        session keeps ONE consistent voice for the entire turn — separate per-
        sentence REST requests drift in timbre/prosody because xAI exposes no seed
        (verified against the live API). Streams PCM as it synthesizes, so TTFT
        stays low. Callers that can't stream still use ``speak`` (REST)."""
        voice_id = resolve_voice(voice, self._settings.tts_voice)
        query = urlencode(
            {
                "language": self._settings.tts_language,
                "voice": voice_id,
                "codec": "pcm",
                "sample_rate": SAMPLE_RATE,
                "optimize_streaming_latency": 2,
            }
        )
        base = self._settings.xai_base_url.replace("https://", "wss://").replace("http://", "ws://")
        ws = await websockets.connect(
            f"{base}/tts?{query}",
            additional_headers={"Authorization": f"Bearer {self._settings.xai_api_key}"},
            open_timeout=self._settings.tts_timeout_s,
        )
        return GrokTTSStream(
            ws,
            on_close=lambda chars: self._log_cost(user_id, session_id, chars),
        )

    async def _synthesize(self, text: str, voice_id: str) -> AsyncIterator[bytes]:
        payload = {
            "text": text,
            "language": self._settings.tts_language,
            "voice_id": voice_id,
            "output_format": {"codec": "pcm", "sample_rate": SAMPLE_RATE},
        }
        headers = {"Authorization": f"Bearer {self._settings.xai_api_key}"}
        async with (
            httpx.AsyncClient(timeout=self._settings.tts_timeout_s) as client,
            client.stream(
                "POST", f"{self._settings.xai_base_url}/tts", headers=headers, json=payload
            ) as response,
        ):
            response.raise_for_status()
            async for audio in response.aiter_bytes():
                yield audio

    def _log_cost(self, user_id: str, session_id: str | None, characters: int) -> None:
        if self._ledger is None or characters == 0:
            return
        self._ledger.log(
            CostEntry(
                user_id=user_id,
                component="tts",
                provider="xai-grok",
                units={"characters": characters},
                cost_usd=round(characters * _COST_PER_CHAR_USD, 6),
                metadata=CostMetadata(session_id=session_id),
            )
        )


class GrokTTSStream:
    """One open xAI WebSocket TTS session for a whole turn (§23, §2b).

    ``feed`` pushes text as it's generated (``text.delta``); ``finish`` flushes
    (``text.done``); ``audio`` yields PCM16 chunks as they stream back. Because it
    is a single session, the voice stays identical across every sentence of the
    reply. Closing (barge-in §24, or turn end) stops synthesis and logs the
    characters submitted (rule 5), billed either way."""

    def __init__(
        self,
        ws: websockets.ClientConnection,
        *,
        on_close: Callable[[int], None],
    ) -> None:
        self._ws = ws
        self._on_close = on_close
        self._chars = 0
        self._closed = False

    async def feed(self, text: str) -> None:
        if self._closed or not text.strip():
            return
        self._chars += len(text)
        await self._ws.send(json.dumps({"type": "text.delta", "delta": text}))

    async def finish(self) -> None:
        """Signal end-of-text so xAI flushes the tail; audio keeps arriving until
        ``audio.done`` (drained by the ``audio`` iterator)."""
        if self._closed:
            return
        await self._ws.send(json.dumps({"type": "text.done"}))

    async def audio(self) -> AsyncIterator[bytes]:
        """Yield PCM16 as it streams; ends on ``audio.done`` or when the socket
        closes (including a barge-in ``aclose``)."""
        try:
            async for message in self._ws:
                if isinstance(message, (bytes, bytearray)):
                    yield bytes(message)
                    continue
                event = json.loads(message)
                kind = event.get("type")
                if kind == "audio.delta":
                    yield base64.b64decode(event["delta"])
                elif kind == "audio.done":
                    break
                elif kind == "error":
                    logger.warning("grok tts stream error: %s", event)
                    break
        except websockets.ConnectionClosed:
            pass

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._ws.close()
        finally:
            self._on_close(self._chars)
