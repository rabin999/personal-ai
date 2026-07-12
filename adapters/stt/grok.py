"""Adapter: xAI Grok Speech-to-Text (implements ports.stt.STT).

xAI shipped a standalone STT API (2026-04) after the original design assumed Grok was
TTS-only. It's a real upgrade over local faster-whisper — vendor-grade accuracy (~5%
WER), word timestamps, 25+ languages, and keyterm biasing. This adapter uses the REST
batch endpoint (``POST https://api.x.ai/v1/stt``) over a VAD-bounded utterance: the
pipeline hands us one utterance's frames, we buffer them and transcribe once, yielding
a single FINAL ``TranscriptPiece`` (the same segmented shape faster-whisper uses).

Selected via ``settings.stt_engine == "grok"``; faster-whisper stays the default ($0,
local). Cost ($0.10/hr batch) is logged to the Cost Ledger by audio seconds. The
real-time WebSocket endpoint (interim results + Smart Turn detection) is a further
upgrade left as a follow-up — this REST adapter proves the swap works end-to-end.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from config.settings import Settings
from core.cost import CostEntry, CostLedger, CostMetadata
from ports.stt import STT, TranscriptPiece, WordConfidence

logger = logging.getLogger(__name__)


async def _once(pcm: bytes) -> AsyncIterator[bytes]:
    """Re-present an already-buffered utterance as a one-shot frame stream for the fallback."""
    yield pcm


SAMPLE_RATE = 16_000  # PCM16 mono the pipeline feeds us
_COST_PER_SECOND_USD = 0.10 / 3600  # xAI Grok STT batch pricing ($0.10/hr)
# Grok STT returns word timestamps but no per-word confidence; it's a high-accuracy
# model, so we attach a uniform high confidence (the §21 clarification gate still
# fires on genuinely empty/short transcripts).
_DEFAULT_WORD_CONFIDENCE = 0.95


class GrokSTT:
    name = "grok-stt"

    def __init__(
        self,
        settings: Settings,
        ledger: CostLedger | None = None,
        fallback: "STT | None" = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        # Local safety net: xAI STT intermittently ReadTimeouts from the box, and a dropped
        # utterance reads to the user as "it didn't hear me" plus a long wait. When the remote
        # call fails we transcribe the SAME audio locally instead of losing the turn.
        self._fallback = fallback

    def preload(self) -> None:
        """Warm the local FALLBACK model at startup so the first time Grok times out, the
        fallback answers fast instead of paying a cold model load then (Grok itself is remote —
        nothing to warm)."""
        if self._fallback is not None:
            self._fallback.preload()

    async def transcribe_stream(
        self,
        frames: AsyncIterator[bytes],
        vocab: list[str] | None = None,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> AsyncIterator[TranscriptPiece]:
        """Buffer the utterance's PCM frames, transcribe once, yield the final piece. If the
        remote call FAILS (timeout/error), transcribe the same audio with the local fallback so
        the utterance is never silently dropped."""
        buffer = bytearray()
        async for frame in frames:
            buffer.extend(frame)
        if not buffer:
            return
        result = await self._transcribe(bytes(buffer), vocab, user_id, session_id)
        if result is None:  # remote failure → don't drop the turn, transcribe locally
            if self._fallback is not None:
                logger.warning("Grok STT failed; falling back to local whisper for this utterance")
                async for piece in self._fallback.transcribe_stream(
                    _once(bytes(buffer)), vocab, user_id=user_id, session_id=session_id
                ):
                    yield piece
            return
        text, words, _dur = result
        if text.strip():
            yield TranscriptPiece(text=text.strip(), words=words, is_final=True)

    async def _transcribe(
        self, pcm: bytes, vocab: list[str] | None, user_id: str, session_id: str | None
    ) -> tuple[str, list[WordConfidence], float] | None:
        # Multipart: raw PCM16 needs audio_format + sample_rate; keyterm biases the
        # user's own names/terms (from Semantic Memory, §20 vocab-boost) so it stops
        # mangling names it's never heard. A dict with a LIST value for keyterm is how
        # httpx sends a repeated form field under an AsyncClient (a list-of-tuples for
        # ``data`` trips httpx's sync-multipart guard — verified against the live API).
        data: dict[str, Any] = {
            "audio_format": "pcm",
            "sample_rate": str(SAMPLE_RATE),
            "format": "true",  # inverse text normalization (numbers/dates readable)
        }
        if self._settings.stt_language:
            data["language"] = self._settings.stt_language
        keyterms = [t.strip() for t in (vocab or []) if t.strip()][:50]
        if keyterms:
            data["keyterm"] = keyterms
        files = {"file": ("utterance.pcm", pcm, "application/octet-stream")}
        headers = {"Authorization": f"Bearer {self._settings.xai_api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self._settings.stt_timeout_s) as client:
                resp = await client.post(
                    f"{self._settings.xai_base_url}/stt", headers=headers, files=files, data=data
                )
                resp.raise_for_status()
                body = resp.json()
        except Exception:
            logger.warning("Grok STT request failed (will fall back locally if configured)")
            return None  # signal FAILURE (vs a genuinely empty transcript) → caller falls back
        text = str(body.get("text") or "")
        duration = float(body.get("duration") or (len(pcm) / (SAMPLE_RATE * 2)))
        words = [
            WordConfidence(
                word=str(w.get("text") or w.get("word") or ""), confidence=_DEFAULT_WORD_CONFIDENCE
            )
            for w in (body.get("words") or [])
            if (w.get("text") or w.get("word"))
        ]
        self._log_cost(user_id, session_id, duration)
        return text, words, duration

    def _log_cost(self, user_id: str, session_id: str | None, seconds: float) -> None:
        if self._ledger is None or seconds <= 0:
            return
        self._ledger.log(
            CostEntry(
                user_id=user_id,
                component="stt",
                provider="xai-grok",
                units={"seconds": round(seconds, 2)},
                cost_usd=round(seconds * _COST_PER_SECOND_USD, 6),
                metadata=CostMetadata(session_id=session_id),
            )
        )
