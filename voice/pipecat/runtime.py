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
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.turns.user_turn_processor import UserTurnProcessor

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
        generator=pipeline.orchestrator,
        working=pipeline.working,
        extractor=pipeline.extractor,
        dispatcher=pipeline.dispatcher,
        make_context=lambda p: _context(user_id, session_id, p),
    )
    tts = CompanionTTSService(pipeline.tts, user_id=user_id, session_id=session_id, voice=voice)
    # Pipecat 1.5 VAD/turn model (spec §19/§21/§24): the VADProcessor emits
    # VADUser{Started,Stopped}SpeakingFrame from Silero; the UserTurnProcessor turns
    # those into UserStarted/StoppedSpeakingFrame + an InterruptionFrame when the user
    # speaks over the reply — which is what actually drives barge-in (§24) and what
    # CompanionProcessor cancels the in-flight reply on. Without the turn processor,
    # VAD fires but nothing interrupts. stop_secs=0.2 is the framework-recommended
    # default the built-in STT latency values assume.
    vad = SileroVADAnalyzer(sample_rate=STT_SAMPLE_RATE, params=VADParams(stop_secs=0.2))
    return Pipeline(
        [
            transport.input(),
            VADProcessor(vad_analyzer=vad),
            UserTurnProcessor(),
            stt,
            companion,
            tts,
            transport.output(),
        ]
    )


async def run_pipeline(pipeline: Pipeline) -> None:
    """Run the assembled pipeline; interruptions/barge-in are framework-driven.

    Barge-in is on by default in Pipecat 1.5 — the UserTurnProcessor (see
    build_pipeline) broadcasts an InterruptionFrame when the user speaks over the
    reply, stopping TTS and — via CompanionProcessor — cancelling the in-flight
    reply (spec §24). The old ``PipelineParams(allow_interruptions=True)`` flag was
    removed in 1.5: passing it did nothing (pydantic silently dropped the unknown
    field), so it must not be relied on.
    """
    task = PipelineTask(pipeline, params=PipelineParams())
    # handle_sigint=False is REQUIRED here: PipelineRunner installs SIGINT/SIGTERM
    # handlers by default, but a WebSocket handler runs inside a uvicorn worker —
    # not the main thread — where asyncio.add_signal_handler raises. Left on, the
    # runner blows up the instant the pipeline starts, the route's except-Exception
    # closes the socket, and the browser's Start button snaps back to idle (the
    # reported prod symptom). The edge already owns process signals.
    await PipelineRunner(handle_sigint=False).run(task)


def _context(user_id: str, session_id: str, prompt: AssembledPrompt) -> ToolContext:
    project_id = None
    if isinstance(prompt, AssembledPrompt):
        for c in prompt.resolved_entities:
            if c.entity_type == "project":
                project_id = c.entity_id
                break
    return ToolContext(user_id=user_id, session_id=session_id, project_id=project_id)
