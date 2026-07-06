"""Voice session runtime — a continuous conversation, start to finish (design §17.1).

Not push-to-talk: once a conversation starts, the user just talks. The runtime
listens continuously and takes turns on its own, exactly as the spec describes —
§19 VAD gate decides speech vs. silence (idle is free), §21 semantic endpointing
decides when the user actually finished (short pause after a complete thought,
long pause after a trailing "and…"/filler), then §10 assembly → §11/§12
generation → §23 TTS produce the reply. If the user speaks while the companion
is talking, that's a barge-in (§24): playback stops and the new utterance starts.

Every stage emits a TraceEvent (grouped per turn by ``turn_index``) so the UI
can show the whole pipeline and replay each reply's audio.
"""

import asyncio
import logging
from collections.abc import AsyncIterator

from core.memory.episodic import EpisodicMemory
from core.memory.working import Turn, WorkingMemory
from core.reasoning.prompt_assembly import DisambiguationRequest, PromptAssembler
from core.reasoning.response_gen import ResponseGenerator
from ports.stt import STT
from ports.tts import TTS
from voice.emotion import LaggingEmotionProvider
from voice.endpointing import SemanticEndpointer
from voice.pipeline import START_FRAMES, AudioInputPipeline, PipelineConfig, VADModel
from voice.trace import TraceEmitter

logger = logging.getLogger(__name__)

TTS_SAMPLE_RATE = 24_000  # §23 gpt-audio pcm16 output
SAMPLE_RATE = 16_000
_MS_PER_BYTE = 1000.0 / (SAMPLE_RATE * 2)  # PCM16 mono


class VoiceSession:
    def __init__(
        self,
        *,
        user_id: str,
        session_id: str,
        vad: VADModel,
        config: PipelineConfig,
        stt: STT,
        endpointer: SemanticEndpointer,
        assembler: PromptAssembler,
        generator: ResponseGenerator,
        tts: TTS,
        working: WorkingMemory,
        trace: TraceEmitter,
        episodic: EpisodicMemory | None = None,
        emotion: LaggingEmotionProvider | None = None,
        voice: str | None = None,
        barge_in: bool = True,
    ) -> None:
        self._user_id = user_id
        self._session_id = session_id
        self._pipeline = AudioInputPipeline(config, vad)
        self._stt = stt
        self._endpointer = endpointer
        self._assembler = assembler
        self._generator = generator
        self._tts = tts
        self._working = working
        self._trace = trace
        self._episodic = episodic
        self._emotion = emotion
        self._voice = voice
        self._barge_in = barge_in

    async def converse(self, frames: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Run a whole conversation over a continuous frame stream.

        Yields TTS PCM16 (24kHz) audio chunks as replies stream. The stream
        ends when ``frames`` is exhausted (client stopped the conversation).
        """
        out: asyncio.Queue[bytes | None] = asyncio.Queue()
        consumer = asyncio.create_task(self._consume(frames, out))
        try:
            while True:
                chunk = await out.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    # ── continuous state machine ─────────────────────────────────────────

    async def _consume(
        self, frames: AsyncIterator[bytes], out: asyncio.Queue[bytes | None]
    ) -> None:
        self._trace.emit("session", "conversation started", user_id=self._user_id)
        buffer: list[bytes] = []
        silence_ms = 0.0
        barge_frames = 0
        decided_incomplete = False
        capturing = False
        turn: asyncio.Task[None] | None = None
        try:
            async for frame in self._pipeline.stream(frames):
                frame_ms = len(frame.pcm) * _MS_PER_BYTE

                if turn is not None and turn.done():
                    turn = None  # reply finished; back to listening

                # Barge-in: user speaks while the companion is talking (§24).
                # Requires START_FRAMES of *fresh* raw speech (not the just-ended
                # utterance's hysteresis tail) so a reply isn't self-interrupted.
                if turn is not None:
                    barge_frames = barge_frames + 1 if frame.is_speech else 0
                    if self._barge_in and barge_frames >= START_FRAMES:
                        self._trace.emit("barge_in", "user interrupted — stopping playback")
                        turn.cancel()
                        await asyncio.gather(turn, return_exceptions=True)
                        turn = None
                        self._drain(out)
                        self._trace.begin_turn()
                        capturing, buffer, silence_ms, barge_frames = True, [frame.pcm], 0.0, 0
                    continue  # replying: ignore our own trailing silence

                if frame.event == "speech_start":
                    self._trace.begin_turn()
                    self._trace.emit("vad", "speech detected — capturing")
                    capturing, buffer, silence_ms, decided_incomplete = True, [], 0.0, False
                if not capturing:
                    continue  # §19 idle gate: nothing paid runs during silence

                buffer.append(frame.pcm)
                if frame.is_speech:  # raw per-frame verdict, not the gate hysteresis
                    silence_ms = 0.0
                    continue

                # Trailing silence inside an utterance → is the thought done? (§21)
                silence_ms += frame_ms
                threshold = (
                    self._endpointer.long_pause_ms
                    if decided_incomplete
                    else self._endpointer.short_pause_ms
                )
                if silence_ms < threshold:
                    continue

                transcript = await self._transcribe(buffer)
                if not transcript.strip():
                    capturing, buffer = False, []
                    continue
                decision = self._endpointer.decide(transcript, silence_ms)
                if not decision.respond:
                    decided_incomplete = True  # wait for the long pause instead
                    continue

                self._trace.emit(
                    "endpoint", f"complete_thought={decision.complete_thought}",
                    complete=decision.complete_thought, threshold_ms=decision.threshold_ms,
                )
                utterance = b"".join(buffer)
                capturing, buffer, silence_ms = False, [], 0.0
                turn = asyncio.create_task(self._run_turn(transcript, utterance, out))
        except asyncio.CancelledError:
            if turn is not None:
                turn.cancel()
            raise
        finally:
            if turn is not None:
                await asyncio.gather(turn, return_exceptions=True)
            out.put_nowait(None)

    def _drain(self, out: asyncio.Queue[bytes | None]) -> None:
        while not out.empty():
            try:
                out.get_nowait()
            except asyncio.QueueEmpty:
                break

    # ── one turn (utterance already endpointed) ──────────────────────────

    async def _run_turn(
        self, transcript: str, utterance: bytes, out: asyncio.Queue[bytes | None]
    ) -> None:
        try:
            self._trace.emit("stt", f"final: {transcript!r}", text=transcript)

            emotion = self._emotion_signal()
            if self._emotion is not None:
                self._emotion.schedule(
                    utterance, user_id=self._user_id, session_id=self._session_id
                )

            self._working.append(self._session_id, Turn(role="user", text=transcript))
            prompt = await self._assembler.assemble(
                self._user_id, self._session_id, transcript, emotion=emotion
            )
            if isinstance(prompt, DisambiguationRequest):
                self._trace.emit(
                    "assembly", "ambiguous reference — asking to disambiguate",
                    candidates=[c.name for c in prompt.candidates[:3]],
                )
            else:
                self._trace.emit(
                    "assembly", f"prompt assembled (complexity={prompt.complexity_hint})",
                    complexity=prompt.complexity_hint,
                    entities=[c.name for c in prompt.resolved_entities],
                )
                self._trace.emit("router", f"routing to {prompt.complexity_hint} tier")

            result = await self._generator.generate(prompt)
            self._trace.emit(
                "generation", f"action={result.action}", action=result.action,
                turn_id=result.turn_id,
            )
            self._trace.emit("response", result.final_text, text=result.final_text)
            self._working.append(
                self._session_id, Turn(role="assistant", text=result.final_text)
            )
            self._remember(transcript, result.final_text)
            await self._synthesize(result.final_text, out)
        except asyncio.CancelledError:
            self._trace.emit("barge_in", "reply cancelled")
            raise
        except Exception as exc:  # never let one turn kill the conversation
            logger.exception("voice turn failed")
            self._trace.emit("error", f"{type(exc).__name__}: {exc}", level="error")

    async def _transcribe(self, speech: list[bytes]) -> str:
        async def _frames() -> AsyncIterator[bytes]:
            for pcm in speech:
                yield pcm

        final_text = ""
        async for piece in self._stt.transcribe_stream(
            _frames(), user_id=self._user_id, session_id=self._session_id
        ):
            if piece.is_final:
                final_text = piece.text
        return final_text

    def _emotion_signal(self) -> dict[str, float | str] | None:
        if self._emotion is None:
            return None
        read = self._emotion.current()
        if read is None:
            return None
        self._trace.emit("emotion", f"acoustic read: {read.label}", **read.model_dump())
        return read.model_dump()

    async def _synthesize(self, text: str, out: asyncio.Queue[bytes | None]) -> None:
        self._trace.emit("tts", "synthesizing reply audio")
        total = 0
        async for chunk in self._tts.speak(
            text, self._voice, user_id=self._user_id, session_id=self._session_id
        ):
            total += len(chunk)
            out.put_nowait(chunk)
        self._trace.emit("tts", f"reply audio complete ({total} bytes)", bytes=total)

    def _remember(self, user_text: str, assistant_text: str) -> None:
        """Persist the turn to episodic memory (§5) for future recall; non-blocking."""
        if self._episodic is None:
            return
        chunk = f"user: {user_text}\nassistant: {assistant_text}"
        task = asyncio.create_task(
            self._episodic.write(self._user_id, self._session_id, [chunk])
        )
        task.add_done_callback(lambda t: t.exception())  # swallow; write is best-effort
