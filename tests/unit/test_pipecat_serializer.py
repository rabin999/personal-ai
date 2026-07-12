"""The raw-PCM serializer bridges our wire protocol to Pipecat: audio → binary PCM,
and (new) live trace events → a JSON text frame on the SAME output channel, so the
Pipecat runtime streams the live transcript the browser renders — parity with native.
"""

import json

import pytest
from pipecat.frames.frames import (
    OutputAudioRawFrame,
    OutputTransportMessageUrgentFrame,
    TextFrame,
    TTSAudioRawFrame,
)

from voice.pipecat.serializer import TTS_SAMPLE_RATE, RawPCMSerializer


async def test_audio_frame_serializes_to_raw_pcm_bytes() -> None:
    s = RawPCMSerializer()
    pcm = b"\x01\x00" * 240
    tts = TTSAudioRawFrame(audio=pcm, sample_rate=TTS_SAMPLE_RATE, num_channels=1)
    out = await s.serialize(tts)
    assert out == pcm
    out2 = await s.serialize(
        OutputAudioRawFrame(audio=pcm, sample_rate=TTS_SAMPLE_RATE, num_channels=1)
    )
    assert out2 == pcm


async def test_trace_message_serializes_to_json_text() -> None:
    s = RawPCMSerializer()
    payload = {"type": "trace", "stage": "reply_chunk", "data": {"voice_text": "hi there"}}
    out = await s.serialize(OutputTransportMessageUrgentFrame(message=payload))
    assert isinstance(out, str)
    assert json.loads(out) == payload


async def test_non_serialized_frames_return_none() -> None:
    s = RawPCMSerializer()
    assert await s.serialize(TextFrame("plain")) is None


async def test_binary_input_deserializes_to_audio_frame() -> None:
    from pipecat.frames.frames import InputAudioRawFrame

    s = RawPCMSerializer()
    frame = await s.deserialize(b"\x00" * 320)
    assert isinstance(frame, InputAudioRawFrame)
    assert await s.deserialize("some text") is None  # control text handled outside the pipeline


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
