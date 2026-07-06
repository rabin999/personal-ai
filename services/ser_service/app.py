"""SER microservice (spec §22): emotion2vec on a small GPU box.

Deployed separately from the core app because it needs a GPU (design doc
§17.3). The heavy model deps (``funasr``/``torch``, the ``ser`` extra) are
imported lazily so this module — and the shared VA mapping used by tests —
imports on any machine. Exposes:

    POST /analyze   octet-stream PCM16 mono 16kHz body → EmotionRead JSON
    GET  /health    readiness (model loaded)

Run:  uv run --extra ser uvicorn services.ser_service.app:app --port 8232
"""

import logging
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, Request

from ports.ser import EmotionRead

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000

# emotion2vec_plus emits scores over these 9 categorical labels; we map each
# to a valence/arousal point on the affect circumplex so the read carries a
# dimensional signal too (§22 interface: valence + arousal + label). Values
# are approximate affect coordinates, not clinical measures (rule 4).
_LABEL_VA: dict[str, tuple[float, float]] = {
    "angry": (-0.7, 0.7),
    "disgusted": (-0.6, 0.3),
    "fearful": (-0.6, 0.6),
    "happy": (0.8, 0.6),
    "neutral": (0.0, 0.0),
    "sad": (-0.7, -0.5),
    "surprised": (0.3, 0.7),
    "other": (0.0, 0.0),
    "unknown": (0.0, 0.0),
}


def scores_to_read(labels: list[str], scores: list[float]) -> EmotionRead:
    """Pick the top-scoring label and project it onto valence/arousal.

    Pure and dependency-free so it is unit-testable without the GPU model.
    """
    if not labels or not scores:
        return EmotionRead(valence=0.0, arousal=0.0, label="neutral", confidence=0.0)
    top_index = max(range(len(scores)), key=lambda i: scores[i])
    label = _normalize_label(labels[top_index])
    valence, arousal = _LABEL_VA.get(label, (0.0, 0.0))
    return EmotionRead(
        valence=valence,
        arousal=arousal,
        label=label,
        confidence=max(0.0, min(1.0, float(scores[top_index]))),
    )


def _normalize_label(raw: str) -> str:
    # emotion2vec labels arrive as e.g. "生气/angry" — keep the english side.
    return raw.split("/")[-1].strip().lower()


@lru_cache(maxsize=1)
def _model() -> Any:
    from funasr import AutoModel  # type: ignore[import-not-found]  # ser extra, GPU box

    logger.info("loading emotion2vec model (first request warms the GPU)...")
    return AutoModel(model="iic/emotion2vec_plus_large", disable_update=True)


def _analyze_pcm16(pcm16: bytes) -> EmotionRead:
    import numpy as np

    audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    result = _model().generate(
        audio, granularity="utterance", extract_embedding=False, sr=SAMPLE_RATE
    )
    first = result[0] if result else {}
    return scores_to_read(first.get("labels", []), first.get("scores", []))


app = FastAPI(title="companion-ser", version="1.0")


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/analyze")
async def analyze(request: Request) -> EmotionRead:
    audio_window = await request.body()
    return _analyze_pcm16(audio_window)
