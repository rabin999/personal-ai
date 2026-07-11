"""Companion reasoning as a Pipecat FrameProcessor (spec §19-24).

Sits in the Pipecat pipeline between STT and TTS: on a FINAL transcription it runs
the full reasoning core — §10 prompt assembly (memory read) → §12 generation with
behavior gates → §1 extraction (memory write, off the reply path) — and pushes the
reply downstream as text for the TTS service to speak. Pipecat owns the transport,
VAD, and barge-in; this processor owns the thinking. Interruptions cancel the
in-flight reply (framework-driven), matching §24.

Kept framework-thin: the reasoning objects are the same ports the native runtime
uses, so both runtimes share one engine.
"""

import asyncio
import contextlib
import logging
from typing import Any, Protocol

from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from core.memory.working import Turn, WorkingMemory
from core.reasoning.prompt_assembly import DisambiguationRequest

logger = logging.getLogger(__name__)


class _Assembler(Protocol):
    async def assemble(self, user_id: str, session_id: str, utterance: str) -> Any: ...


class _Generator(Protocol):
    async def generate(self, prompt: Any, dispatcher: Any = None, context: Any = None) -> Any: ...

    async def generate_spoken(
        self, prompt: Any, dispatcher: Any, context: Any, speak: Any
    ) -> Any: ...


class _Extractor(Protocol):
    async def extract_and_store(
        self, user_id: str, session_id: str, user_text: str, assistant_text: str
    ) -> Any: ...


class CompanionProcessor(FrameProcessor):
    def __init__(
        self,
        *,
        user_id: str,
        session_id: str,
        assembler: _Assembler,
        generator: _Generator,
        working: WorkingMemory,
        extractor: _Extractor | None = None,
        dispatcher: Any = None,
        make_context: Any = None,
        logs: Any = None,
        traces: Any = None,
        evaluator: Any = None,
    ) -> None:
        super().__init__()
        self._user_id = user_id
        self._session_id = session_id
        self._assembler = assembler
        self._generator = generator
        self._working = working
        self._extractor = extractor
        self._dispatcher = dispatcher
        self._make_context = make_context
        # C1 parity with the native runtime: bind correlation ids per turn so the
        # deep per-LLM-call spans persist, and persist stage spans so a Pipecat turn
        # shows in the /conversations Trace tab; score it with the judge (Langfuse).
        self._logs = logs
        self._traces = traces
        self._evaluator = evaluator
        self._turn = 0
        self._reply_task: asyncio.Task[None] | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        # Barge-in (§24): the framework's VAD emits an InterruptionFrame when the
        # user speaks over the reply (pipecat 1.5 renamed StartInterruptionFrame →
        # InterruptionFrame). Cancel the in-flight reply so we stop thinking about
        # (and speaking) the abandoned turn — matching the native runtime, which
        # cancels its turn task. TTS is stopped by the framework; this cancels OUR
        # generation so a late TextFrame can't be pushed after the interruption.
        if isinstance(frame, InterruptionFrame):
            self._cancel_reply()
            await self.push_frame(frame, direction)
            return
        # Act on a FINAL user transcription (TranscriptionFrame, not the interim
        # partials); pass everything else through so Pipecat's system/control frames
        # (start/end/interruption) flow normally. We consume the transcription so
        # the user's own words are never forwarded to TTS. The reply runs as a
        # cancellable task (not awaited inline) so an interruption mid-generation
        # actually stops it.
        if isinstance(frame, TranscriptionFrame) and not isinstance(
            frame, InterimTranscriptionFrame
        ):
            self._cancel_reply()  # a new final transcript supersedes any in-flight reply
            self._reply_task = asyncio.create_task(self._respond(frame.text))
            self._reply_task.add_done_callback(self._on_reply_done)
        else:
            await self.push_frame(frame, direction)

    def _cancel_reply(self) -> None:
        if self._reply_task is not None and not self._reply_task.done():
            self._reply_task.cancel()
        self._reply_task = None

    def _on_reply_done(self, task: "asyncio.Task[None]") -> None:
        with contextlib.suppress(asyncio.CancelledError):
            task.exception()  # retrieve to avoid "never retrieved" warnings

    async def _respond(self, text: str) -> None:
        if not text.strip():
            return
        self._turn += 1
        turn = self._turn
        # Bind so every LLM call this turn logs its deep span into this trace (C1).
        scope = (
            self._logs.bind(trace_id=self._session_id, turn_id=turn, user_id=self._user_id)
            if self._logs is not None
            else contextlib.nullcontext()
        )
        with scope:
            try:
                self._working.append(self._session_id, Turn(role="user", text=text))
                await self._trace(turn, "session", "voice turn", text=text)
                prompt = await self._assembler.assemble(self._user_id, self._session_id, text)
                await self.push_frame(LLMFullResponseStartFrame())
                if isinstance(prompt, DisambiguationRequest):
                    names = " or ".join(f'"{c.name}"' for c in prompt.candidates[:3])
                    reply = f"Quick check — do you mean {names}?"
                    await self.push_frame(TextFrame(reply))
                else:
                    context = self._make_context(prompt) if self._make_context else None

                    # STREAM the reply into TTS sentence-by-sentence (§8.12): the first
                    # sentence starts synthesizing while the rest is still generating —
                    # low time-to-first-audio. (Was: generate the WHOLE reply, then push
                    # one TextFrame → TTS waited for the entire turn → felt slow.)
                    async def _speak(sentence: str) -> None:
                        await self.push_frame(TextFrame(sentence))

                    result = await self._generator.generate_spoken(
                        prompt, self._dispatcher, context, _speak
                    )
                    reply = result.final_text
                await self.push_frame(LLMFullResponseEndFrame())
                self._working.append(self._session_id, Turn(role="assistant", text=reply))
                await self._trace(turn, "response", reply, text=reply)
                self._remember(text, reply)
                if self._evaluator is not None:
                    self._evaluator.schedule(
                        session_id=self._session_id, turn=turn, user_msg=text, reply=reply
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # never let one turn tear down the pipeline
                logger.exception("companion turn failed")

    async def _trace(self, turn: int, stage: str, message: str, **data: Any) -> None:
        """Persist one stage span so a Pipecat turn shows in the Trace tab (best-effort)."""
        if self._traces is None:
            return
        try:
            await self._traces.record(
                self._user_id,
                {
                    "session_id": self._session_id,
                    "turn": turn,
                    "stage": stage,
                    "message": message,
                    "data": data,
                },
            )
        except Exception:
            logger.debug("pipecat trace persist failed", exc_info=True)

    def _remember(self, user_text: str, assistant_text: str) -> None:
        """Run the §1 extraction write step off the reply path (best-effort)."""
        if self._extractor is None:
            return
        task = asyncio.create_task(
            self._extractor.extract_and_store(
                self._user_id, self._session_id, user_text, assistant_text
            )
        )
        task.add_done_callback(lambda t: t.exception())
