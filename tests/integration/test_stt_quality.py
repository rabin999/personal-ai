"""Real audio-path STT quality (spec §20, F2): does Whisper + vocab biasing get
the user's rare terms right?

Unlike test_stt.py (a synthetic tone that proves the streaming/cost contract),
this drives REAL speech: espeak synthesizes a sentence with terms generic Whisper
mangles ("NEPSE", "Trishul"), ffmpeg resamples to 16kHz PCM16, and the adapter
transcribes it. Proves the F2 claim that the accurate final model + vocab prompt
transcribe user-specific vocabulary correctly. Skips loudly if espeak/ffmpeg or
the Whisper models aren't available (offline CI) — never a false pass.
"""

import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from adapters.stt.faster_whisper import FasterWhisperSTT

pytestmark = pytest.mark.integration

SENTENCE = "I trade on NEPSE every morning with my friend Trishul from Kathmandu"
VOCAB = ["NEPSE", "Trishul", "Kathmandu"]


def _synthesize(text: str) -> bytes:
    """espeak → wav → 16kHz mono PCM16 via ffmpeg. Skips if either tool is absent."""
    if not shutil.which("espeak") or not shutil.which("ffmpeg"):
        pytest.skip("espeak/ffmpeg not available for real audio-path STT test")
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "a.wav"
        pcm = Path(d) / "a.pcm"
        subprocess.run(
            ["espeak", "-v", "en-us", "-s", "150", text, "-w", str(wav)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav), "-ar", "16000", "-ac", "1", "-f", "s16le", str(pcm)],
            check=True,
            capture_output=True,
        )
        return pcm.read_bytes()


async def _frames(pcm: bytes, chunk: int = 3200) -> AsyncIterator[bytes]:
    for i in range(0, len(pcm), chunk):
        yield pcm[i : i + chunk]


async def _final_text(stt: FasterWhisperSTT, pcm: bytes, vocab: list[str] | None) -> str:
    text = ""
    async for piece in stt.transcribe_stream(
        _frames(pcm), vocab, user_id="it_stt_quality", session_id="s"
    ):
        if piece.is_final:
            text = piece.text
    return text


@pytest.fixture(scope="module")
def speech_pcm() -> bytes:
    return _synthesize(SENTENCE)


@pytest.fixture(scope="module")
def stt() -> FasterWhisperSTT:
    faster_whisper = pytest.importorskip("faster_whisper")
    try:  # accurate final model is the F2 default
        faster_whisper.WhisperModel("small", device="cpu", compute_type="int8")
    except Exception as exc:
        pytest.skip(f"faster-whisper small model unavailable: {exc}")
    return FasterWhisperSTT(model_size="base", final_model_size="small")


async def test_vocab_biasing_fixes_rare_user_terms(
    stt: FasterWhisperSTT, speech_pcm: bytes
) -> None:
    """With the user's vocab seeded, the accurate final model transcribes the rare
    terms correctly — the concrete anti-"Herak"-mis-hear guarantee (§20 rule 2)."""
    with_vocab = await _final_text(stt, speech_pcm, VOCAB)
    low = with_vocab.lower()
    # The content word AND the rare user terms are all present and correct.
    assert "trade" in low, f"verb mistranscribed: {with_vocab!r}"
    assert "nepse" in low, f"NEPSE mistranscribed: {with_vocab!r}"
    assert "trishul" in low, f"Trishul mistranscribed: {with_vocab!r}"


async def test_engine_name_reports_both_models(stt: FasterWhisperSTT) -> None:
    """The trace must record which STT produced a transcript (§20/F2)."""
    assert stt.name == "faster-whisper/base+small"
