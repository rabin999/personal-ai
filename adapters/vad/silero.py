"""Adapter: Silero VAD (implements voice.pipeline.VADModel, spec §19).

Wraps the Silero model bundled with the pipecat ``voice`` extra, but calls it
correctly: Silero at 16kHz requires **exactly 512 samples** per inference and
returns a 2-D array — pipecat's own ``voice_confidence`` mis-indexes that and
silently returns 0, so we call the model directly and extract the scalar with
``.item()``. Frames are the runtime's cost gate (idle is free).
"""

import logging
from functools import cached_property
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 512  # Silero's only supported window at 16kHz
FRAME_BYTES = FRAME_SAMPLES * 2  # PCM16


class SileroVAD:
    """Per-utterance voice-activity detector; one instance per session (stateful)."""

    @cached_property
    def _model(self) -> Any:
        from pipecat.audio.vad.silero import SileroVADAnalyzer

        # The analyzer downloads/loads the ONNX model; we borrow that model and
        # drive it ourselves (its public voice_confidence is broken for this
        # model version — mis-indexes the 2-D output).
        return SileroVADAnalyzer(sample_rate=SAMPLE_RATE)._model

    def voice_confidence(self, buffer: bytes) -> float:
        audio = np.frombuffer(buffer, dtype=np.int16).astype(np.float32) / 32768.0
        if audio.shape[0] < FRAME_SAMPLES:
            return 0.0
        try:
            output = self._model(audio[:FRAME_SAMPLES], SAMPLE_RATE)
            return float(np.asarray(output).reshape(-1)[0])
        except Exception:
            logger.exception("Silero VAD inference failed")
            return 0.0
