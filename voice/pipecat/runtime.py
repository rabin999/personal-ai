"""Pipecat voice runtime (CLAUDE.md §5): the framework owns VAD, endpointing, barge-in.

Assembles the pipeline the design asks for — Pipecat's transport + VAD + interruption
instead of a hand-rolled loop:

    transport.input() → VADProcessor(Silero) → STT → Companion(reasoning) → TTS → transport.output()

The transport is Pipecat's FastAPI-WebSocket transport over the browser socket; the
raw-PCM serializer keeps the existing wire protocol. STT/TTS/reasoning are our own
adapters wrapped as Pipecat processors, so both runtimes share one engine. Barge-in
is framework-driven: when the VAD hears the user during playback, Pipecat interrupts
the in-flight reply (matching §24) — no hand-wiring.

Selected via ``settings.voice_runtime == "pipecat"``; the native runtime remains the
default until this is verified end-to-end in the browser.
"""

from typing import Any

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from core.reasoning.prompt_assembly import AssembledPrompt
from core.tools.registry import ToolContext
from voice.pipecat.companion_processor import CompanionProcessor
from voice.pipecat.serializer import STT_SAMPLE_RATE, TTS_SAMPLE_RATE, RawPCMSerializer
from voice.pipecat.services import CompanionSTTService, CompanionTTSService


def build_transport(websocket: Any) -> FastAPIWebsocketTransport:
    """Pipecat FastAPI-WebSocket transport over the browser socket, raw-PCM wire."""
    params = FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=STT_SAMPLE_RATE,
        audio_out_sample_rate=TTS_SAMPLE_RATE,
        add_wav_header=False,
        serializer=RawPCMSerializer(),
    )
    return FastAPIWebsocketTransport(websocket=websocket, params=params)


def build_pipeline(
    transport: FastAPIWebsocketTransport,
    *,
    user_id: str,
    session_id: str,
    pipeline: Any,
    voice: str | None = None,
) -> Pipeline:
    """Wire input → VAD → STT → reasoning → TTS → output with our engine (``pipeline``)."""
    stt = CompanionSTTService(pipeline.stt, user_id=user_id, session_id=session_id)
    companion = CompanionProcessor(
        user_id=user_id,
        session_id=session_id,
        assembler=pipeline.assembler,
        generator=pipeline.generator,
        working=pipeline.working,
        extractor=pipeline.extractor,
        dispatcher=pipeline.dispatcher,
        make_context=lambda p: _context(user_id, session_id, p),
    )
    tts = CompanionTTSService(pipeline.tts, user_id=user_id, session_id=session_id, voice=voice)
    return Pipeline(
        [
            transport.input(),
            VADProcessor(vad_analyzer=SileroVADAnalyzer(sample_rate=STT_SAMPLE_RATE)),
            stt,
            companion,
            tts,
            transport.output(),
        ]
    )


async def run_pipeline(pipeline: Pipeline) -> None:
    """Run the assembled pipeline; interruptions/barge-in are framework-driven.

    ``allow_interruptions=True`` is what actually turns barge-in on: without it
    Pipecat keeps speaking over the user (its default is False), so the user
    talking mid-reply is ignored until the bot finishes (spec §24). With it on,
    the VAD hearing the user during playback emits a StartInterruptionFrame that
    stops TTS and — via CompanionProcessor — cancels the in-flight reply.
    """
    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))
    await PipelineRunner().run(task)


def _context(user_id: str, session_id: str, prompt: AssembledPrompt) -> ToolContext:
    project_id = None
    if isinstance(prompt, AssembledPrompt):
        for c in prompt.resolved_entities:
            if c.entity_type == "project":
                project_id = c.entity_id
                break
    return ToolContext(user_id=user_id, session_id=session_id, project_id=project_id)
