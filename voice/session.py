"""Voice session runtime — one live turn, start to finish (design doc §17.1).

Assembles the real modules into the conversation path a user actually speaks
through: §19 VAD gate (idle is free) → §20 STT → §21 endpointing → §10 prompt
assembly → §11/§12 generation with behavior gates → §23 TTS, with §24
barge-in and one-turn-behind §22 emotion. Every stage emits a TraceEvent so
the UI can show exactly what happened.

The turn boundary is push-to-talk: the caller feeds the utterance's audio
frames and closes the stream when the user stops. The VAD gate still runs so
idle frames never reach paid work and the trace shows speech detection.
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
from voice.bargein import BargeInController
from voice.emotion import LaggingEmotionProvider
from voice.endpointing import SemanticEndpointer
from voice.pipeline import AudioInputPipeline, PipelineConfig, VADModel
from voice.trace import TraceEmitter

logger = logging.getLogger(__name__)

TTS_SAMPLE_RATE = 24_000  # §23 gpt-audio pcm16 output


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
        self._bargein = BargeInController()

    async def on_barge_in(self) -> None:
        """User spoke during playback (§24): stop output, protect any write."""
        self._trace.emit("barge_in", "user interrupted — stopping playback")
        await self._bargein.on_user_speech(self._session_id)

    async def run_turn(self, frames: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Drive one turn; yields TTS PCM16 (24kHz) audio chunks as they stream."""
        self._bargein.attach_generation(asyncio.current_task())  # type: ignore[arg-type]
        try:
            async for chunk in self._run_turn(frames):
                yield chunk
        except asyncio.CancelledError:
            self._trace.emit("barge_in", "turn cancelled")
            raise
        except Exception as exc:  # never let one turn kill the session
            logger.exception("voice turn failed")
            self._trace.emit("error", f"{type(exc).__name__}: {exc}", level="error")

    # ── the pipeline ─────────────────────────────────────────────────────

    async def _run_turn(self, frames: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        self._trace.emit("session", "turn started", user_id=self._user_id)

        # §19 — gate: only speech-active frames reach the paid path.
        speech = await self._gate(frames)
        if not speech:
            self._trace.emit("vad", "no speech detected — idle is free (nothing paid ran)")
            return

        # §20 — STT: windowed partials, then a final with per-word confidence.
        transcript = await self._transcribe(speech)
        if not transcript.strip():
            self._trace.emit("stt", "empty transcript — nothing to respond to")
            return

        # §21 — endpointing decision (push-to-talk already ended the turn).
        decision = self._endpointer.decide(
            transcript, silence_ms=self._endpointer.short_pause_ms
        )
        self._trace.emit(
            "endpoint", f"complete_thought={decision.complete_thought}",
            complete=decision.complete_thought, threshold_ms=decision.threshold_ms,
        )

        # §22 — emotion signal from the previous turn (one turn behind); schedule this one.
        emotion = self._emotion_signal()
        if self._emotion is not None:
            self._emotion.schedule(
                b"".join(speech), user_id=self._user_id, session_id=self._session_id
            )

        # §10 — assemble the prompt over real memory/config.
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

        # §11/§12 — generate with behavior gates + validated JSON judgment.
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

        # §23 — TTS: stream interruptible PCM16 audio back.
        async for chunk in self._synthesize(result.final_text):
            yield chunk

    async def _gate(self, frames: AsyncIterator[bytes]) -> list[bytes]:
        speech: list[bytes] = []
        async for frame in self._pipeline.stream(frames):
            if frame.event == "speech_start":
                self._trace.emit("vad", "speech detected — opening the gate")
            elif frame.event == "speech_end":
                self._trace.emit("vad", "silence — closing the gate")
            if frame.speech_active:
                speech.append(frame.pcm)
        return speech

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
                weak = [w.word for w in piece.words if w.confidence < 0.5]
                self._trace.emit(
                    "stt", f"final: {piece.text!r}",
                    text=piece.text, low_confidence_words=weak,
                )
            elif piece.text:
                self._trace.emit("stt", f"partial: {piece.text!r}", partial=piece.text)
        return final_text

    def _emotion_signal(self) -> dict[str, float | str] | None:
        if self._emotion is None:
            return None
        read = self._emotion.current()
        if read is None:
            return None
        self._trace.emit("emotion", f"acoustic read: {read.label}", **read.model_dump())
        return read.model_dump()

    async def _synthesize(self, text: str) -> AsyncIterator[bytes]:
        self._trace.emit("tts", "synthesizing reply audio")
        stream = self._tts.speak(
            text, self._voice, user_id=self._user_id, session_id=self._session_id
        )
        total = 0
        async for chunk in stream:
            total += len(chunk)
            yield chunk
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
