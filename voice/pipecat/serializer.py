"""Raw PCM16 frame serializer for the browser WebSocket (spec §19).

Keeps the existing browser wire protocol: the client sends raw PCM16 mono @16kHz
binary frames and plays raw PCM16 @24kHz back (the AudioWorklet already does this
for the native runtime), so adopting Pipecat needs no Pipecat JS client — this
serializer bridges those raw bytes to Pipecat's InputAudioRawFrame / audio-out
frames. Non-audio frames are not serialized (control travels as separate JSON).
"""

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    TTSAudioRawFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

STT_SAMPLE_RATE = 16_000
TTS_SAMPLE_RATE = 24_000


class RawPCMSerializer(FrameSerializer):
    def __init__(self, in_rate: int = STT_SAMPLE_RATE, out_rate: int = TTS_SAMPLE_RATE) -> None:
        super().__init__()
        self._in_rate = in_rate
        self._out_rate = out_rate

    async def serialize(self, frame: Frame) -> str | bytes | None:
        # Only audio goes out over the binary channel; the caller streams trace
        # JSON separately. Raw PCM bytes straight to the browser player.
        if isinstance(frame, (TTSAudioRawFrame, OutputAudioRawFrame)):
            return bytes(frame.audio)
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, (bytes, bytearray)):
            return InputAudioRawFrame(audio=bytes(data), sample_rate=self._in_rate, num_channels=1)
        return None  # text/control handled outside the pipeline
