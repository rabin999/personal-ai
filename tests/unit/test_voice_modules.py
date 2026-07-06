"""Unit tests for §20 STT streaming, §21 endpointing, §23 TTS chunking, §24 barge-in."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from adapters.stt.faster_whisper import FasterWhisperSTT
from adapters.tts.grok import chunk_for_synthesis
from ports.stt import TranscriptPiece, WordConfidence
from voice.bargein import BargeInController
from voice.endpointing import SemanticEndpointer, is_complete_thought

# ── §21 semantic endpointing ─────────────────────────────────────────────

endpointer = SemanticEndpointer(short_pause_ms=700, long_pause_ms=2500)


# Acceptance: trailing "and…" + 2s pause does NOT respond.
def test_incomplete_thought_waits_through_a_long_pause() -> None:
    assert not endpointer.should_respond("I was thinking about the parser and", 2000)


# Acceptance: complete sentence + 0.8s pause DOES respond.
def test_complete_thought_responds_after_short_pause() -> None:
    assert endpointer.should_respond("I was thinking about the parser.", 800)


# Acceptance: a filler right before silence never endpoints.
def test_filler_before_silence_blocks_endpointing() -> None:
    assert not endpointer.should_respond("so what I want is, um", 2000)
    assert not endpointer.should_respond("maybe we could, uh", 2400)


def test_incomplete_eventually_endpoints_after_long_pause() -> None:
    assert endpointer.should_respond("I was thinking about the parser and", 2600)


def test_rising_prosody_defers_even_complete_sentences() -> None:
    decision = endpointer.decide("I finished the report.", 800, prosody_rising=True)
    assert not decision.respond and decision.threshold_ms == 2500


def test_completeness_heuristics() -> None:
    assert is_complete_thought("let's cook pasta tonight.")
    assert is_complete_thought("what do you think")
    assert not is_complete_thought("I want to")
    assert not is_complete_thought("she said that,")
    assert not is_complete_thought("well because")
    assert not is_complete_thought("")


def test_empty_transcript_never_endpoints() -> None:
    assert not endpointer.should_respond("", 5000)


# ── §23 TTS chunking (rule 3: tags never split) ──────────────────────────


def test_chunks_break_at_clause_boundaries() -> None:
    text = (
        "That's a big step, and honestly you handled it well. "
        "Want to talk about what happens next? I'm here either way."
    )
    chunks = chunk_for_synthesis(text, max_chars=60)
    assert len(chunks) >= 2
    assert " ".join(chunks).replace("  ", " ") == text


def test_tags_are_never_split_across_chunks() -> None:
    text = (
        "[sigh] That sounds exhausting, truly. <pause> But listen, "
        "[whisper] you did the right thing <emphasis> and it mattered."
    )
    for max_chars in (20, 40, 60, 90):
        chunks = chunk_for_synthesis(text, max_chars=max_chars)
        for tag in ("[sigh]", "<pause>", "[whisper]", "<emphasis>"):
            assert sum(chunk.count(tag) for chunk in chunks) == 1
            assert any(tag in chunk for chunk in chunks)


def test_single_short_text_is_one_chunk() -> None:
    assert chunk_for_synthesis("hey there!") == ["hey there!"]


# ── §20 STT streaming shape (decoder faked) ──────────────────────────────


class FakeDecodedSTT(FasterWhisperSTT):
    """Overrides the model-bound methods to avoid loading whisper weights."""

    def __init__(self) -> None:
        super().__init__(ledger=None, partial_window_s=0.01)
        self.decode_calls = 0

    async def _decode_text(self, audio: bytes, prompt: str | None) -> str:
        self.decode_calls += 1
        return f"partial {self.decode_calls}"

    async def _decode_final(self, audio: bytes, prompt: str | None) -> TranscriptPiece:
        return TranscriptPiece(
            text="final transcript",
            words=[
                WordConfidence(word="final", confidence=0.98),
                WordConfidence(word="transcript", confidence=0.42),
            ],
            is_final=True,
        )


async def _pcm_frames(count: int, frame_bytes: int = 640) -> AsyncIterator[bytes]:
    for _ in range(count):
        yield b"\x00" * frame_bytes


# Acceptance: partials before final; low word confidence surfaced.
async def test_stt_emits_partials_then_final_with_word_confidence() -> None:
    stt = FakeDecodedSTT()
    pieces = [piece async for piece in stt.transcribe_stream(_pcm_frames(20), user_id="u_demo_001")]
    assert len(pieces) >= 2
    assert all(not piece.is_final for piece in pieces[:-1])
    final = pieces[-1]
    assert final.is_final and final.text == "final transcript"
    low = [w for w in final.words if w.confidence < 0.5]
    assert low and low[0].word == "transcript"  # caller sees the weak word


async def test_stt_empty_stream_yields_nothing() -> None:
    stt = FakeDecodedSTT()

    async def no_frames() -> AsyncIterator[bytes]:
        return
        yield b""  # pragma: no cover

    pieces = [p async for p in stt.transcribe_stream(no_frames(), user_id="u")]
    assert pieces == []


# ── §24 barge-in ─────────────────────────────────────────────────────────


async def _hang_forever() -> Any:
    await asyncio.sleep(30)


async def test_barge_in_stops_tts_and_generation_immediately() -> None:
    controller = BargeInController()
    tts = asyncio.create_task(_hang_forever())
    generation = asyncio.create_task(_hang_forever())
    controller.attach_tts(tts)
    controller.attach_generation(generation)

    await controller.on_user_speech("s1")
    await asyncio.sleep(0)

    assert tts.cancelled() and generation.cancelled()
    assert controller.interrupts_handled == 1


# Acceptance: interrupting during an action write defers, never corrupts.
async def test_barge_in_waits_for_in_flight_write() -> None:
    controller = BargeInController()
    tts = asyncio.create_task(_hang_forever())
    controller.attach_tts(tts)
    write_completed = False

    async def do_write() -> None:
        nonlocal write_completed
        async with controller.protected_write():
            await asyncio.sleep(0.1)
            write_completed = True

    write_task = asyncio.create_task(do_write())
    await asyncio.sleep(0.02)  # write is mid-flight
    interrupt = asyncio.create_task(controller.on_user_speech("s1"))
    await asyncio.sleep(0.02)
    assert not interrupt.done()  # deferred behind the write
    assert not tts.cancelled()

    await write_task
    await interrupt
    await asyncio.sleep(0)  # let the cancellation propagate

    assert write_completed  # the write finished untouched
    assert tts.cancelled()  # then the interrupt ran


async def test_interrupt_with_nothing_playing_is_harmless() -> None:
    controller = BargeInController()
    await controller.on_user_speech("s1")
    assert controller.interrupts_handled == 1
