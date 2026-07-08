"""Voice sample preview (spec §23; brief §3.2).

Lets a user hear each Grok voice before choosing. ``GET /api/voices`` lists the
available voices; ``GET /api/voices/{voice}/sample`` synthesizes a short line in
that voice and returns it as a playable WAV (browsers can't play raw PCM16).
Auth'd; the synthesis cost is logged by the TTS adapter (§3).
"""

import io
import wave
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from adapters.tts.grok import SAMPLE_RATE, VOICES
from api.deps import CurrentUser

router = APIRouter(prefix="/api")

_SAMPLE_LINE = "Hey — this is how I sound. Good to meet you."


def _pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # PCM16
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


@router.get("/voices")
async def list_voices(user: CurrentUser, request: Request) -> dict[str, Any]:
    """The full live Grok voice roster (id + name + gender) for the picker (#19)."""
    pipeline = request.app.state.pipeline
    if pipeline is not None:
        try:
            return {"voices": await pipeline.tts.list_voices()}
        except Exception:  # never fail the settings UI on a catalog hiccup
            pass
    return {"voices": [{"voice_id": v, "name": v.title(), "gender": ""} for v in VOICES]}


@router.get("/voices/{voice}/sample")
async def voice_sample(voice: str, user: CurrentUser, request: Request) -> Response:
    if voice.lower() not in set(VOICES):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown voice")
    pipeline = request.app.state.pipeline
    if pipeline is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="pipeline not wired"
        )
    pcm = bytearray()
    async for chunk in pipeline.tts.speak(
        _SAMPLE_LINE, voice, user_id=user.user_id, session_id="voice_preview"
    ):
        pcm.extend(chunk)
    if not pcm:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="TTS returned no audio")
    wav = _pcm16_to_wav(bytes(pcm), SAMPLE_RATE)
    return Response(content=wav, media_type="audio/wav")
