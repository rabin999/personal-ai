"""Adapter: client for the emotion2vec GPU service (implements ports.ser.SER, §22).

The heavy acoustic model runs in a separate microservice on a small GPU box
(design doc §17.3) — different hardware than the rest of the system. This
adapter is the thin client the voice runtime holds: it POSTs the utterance's
PCM16 audio to ``{ser_service_url}/analyze`` and validates the reply into an
``EmotionRead`` (invariant 5: never trust unvalidated JSON — on a bad
response, retry once then fall back to the neutral read).

SER is a self-hosted fixed-cost service (no per-call money cost), so — unlike
the metered STT/TTS providers (§20/§23) — it writes no Cost Ledger entry.
When ``ser_service_url`` is unset, SER is disabled and ``analyze`` returns
None (acoustic emotion deferred; the orchestrator then derives a TEXT-SENTIMENT
read via ``core.reasoning.prosody.emotion_from_text`` so prosody still varies).
"""

import logging

import httpx
from pydantic import ValidationError

from config.settings import Settings
from ports.ser import NEUTRAL_READ, EmotionRead

logger = logging.getLogger(__name__)


class Emotion2VecSER:
    def __init__(self, settings: Settings) -> None:
        self._url = settings.ser_service_url.rstrip("/")
        self._timeout = settings.ser_timeout_s

    async def analyze(
        self,
        audio_window: bytes,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> EmotionRead | None:
        if not self._url:
            return None  # SER disabled — acoustic emotion deferred (design §17.3)
        for attempt in (1, 2):  # invariant 5: validate, retry once, then safe fallback
            try:
                return await self._request(audio_window)
            except (httpx.HTTPError, ValidationError, ValueError) as exc:
                logger.warning("SER analyze failed (attempt %d): %s", attempt, exc)
        return NEUTRAL_READ  # signal, not ground truth — never fabricate an emotion

    async def _request(self, audio_window: bytes) -> EmotionRead:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._url}/analyze",
                content=audio_window,
                headers={"Content-Type": "application/octet-stream"},
            )
            response.raise_for_status()
            return EmotionRead.model_validate(response.json())
