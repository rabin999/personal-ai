"""Companion reasoning as a Pipecat FrameProcessor (CLAUDE.md §5).

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
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    StartInterruptionFrame,
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
        self._reply_task: asyncio.Task[None] | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        # Barge-in (§24): the framework's VAD emits StartInterruptionFrame when the
        # user speaks over the reply. Cancel the in-flight reply so we stop thinking
        # about (and speaking) the abandoned turn — matching the native runtime,
        # which cancels its turn task. TTS is stopped by the framework; this cancels
        # OUR generation so a late TextFrame can't be pushed after the interruption.
        if isinstance(frame, StartInterruptionFrame):
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
        try:
            self._working.append(self._session_id, Turn(role="user", text=text))
            prompt = await self._assembler.assemble(self._user_id, self._session_id, text)
            if isinstance(prompt, DisambiguationRequest):
                names = " or ".join(f'"{c.name}"' for c in prompt.candidates[:3])
                reply = f"Quick check — do you mean {names}?"
            else:
                context = self._make_context(prompt) if self._make_context else None
                result = await self._generator.generate(prompt, self._dispatcher, context)
                reply = result.final_text
            self._working.append(self._session_id, Turn(role="assistant", text=reply))
            # Bracket the reply so the TTS service aggregates + speaks it as one turn.
            await self.push_frame(LLMFullResponseStartFrame())
            await self.push_frame(TextFrame(reply))
            await self.push_frame(LLMFullResponseEndFrame())
            self._remember(text, reply)
        except Exception:  # never let one turn tear down the pipeline
            logger.exception("companion turn failed")

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
