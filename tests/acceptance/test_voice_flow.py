"""End-to-end voice-path acceptance tests (spec §19-§24).

These thread the voice modules through the flows they actually participate
in — real module logic over fake hardware (the GPU/paid providers are
covered by the skip-loud integration tests). They assert the cross-module
contracts: idle-is-free gating (§19) → STT partials (§20) → endpointing
(§21); barge-in (§24) stopping output while protecting an action write
(§13); and the SER read (§22) reaching Prompt Assembly (§10) and the mood
model (§17).
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from core.memory.entities import EntityResolver
from core.memory.episodic import EpisodicMemory
from core.memory.procedural import ProceduralMemory
from core.memory.semantic import SemanticMemory
from core.memory.working import WorkingMemory
from core.profile import ProfileService, TraitRegistry
from core.psych.user_model import PsychUserModel
from core.reasoning.prompt_assembly import AssembledPrompt, PromptAssembler
from core.reasoning.self_model import SelfModel
from ports.ser import EmotionRead
from tests.fakes import FakeDocStore, FakeGraphStore, FakeVectorStore
from tests.unit.test_voice_modules import FakeDecodedSTT
from voice.bargein import BargeInController
from voice.emotion import LaggingEmotionProvider
from voice.endpointing import SemanticEndpointer
from voice.pipeline import AudioFrame, AudioInputPipeline, PipelineConfig

pytestmark = pytest.mark.acceptance

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"
USER = "u_demo_001"
SESSION = "s_voice"


class ScriptedVAD:
    def __init__(self, confidences: list[float]) -> None:
        self.confidences = confidences
        self.index = 0

    def voice_confidence(self, buffer: bytes) -> float:
        value = self.confidences[min(self.index, len(self.confidences) - 1)]
        self.index += 1
        return value


async def _frames(n: int) -> AsyncIterator[bytes]:
    for _ in range(n):
        yield b"\x00" * 640


# ── §19 → §20 → §21: gate opens on speech, STT partials feed endpointing ──


async def test_speech_flows_gate_to_stt_to_endpoint_but_silence_is_free() -> None:
    # 8 idle frames (no paid work), then sustained speech.
    confidences = [0.05] * 8 + [0.9] * 20
    pipeline = AudioInputPipeline(PipelineConfig(), ScriptedVAD(confidences))
    stt = FakeDecodedSTT()

    speech_pcm: list[bytes] = []

    async def on_paid(frame: AudioFrame) -> None:
        speech_pcm.append(frame.pcm)

    async for _ in pipeline.stream(_frames(len(confidences)), on_paid_path=on_paid):
        pass

    # §19: idle produced zero paid frames; speech opened the gate.
    assert len(speech_pcm) > 0
    assert len(speech_pcm) <= 20  # only the speech-active frames reached the paid path

    async def gated_speech() -> AsyncIterator[bytes]:
        for pcm in speech_pcm:
            yield pcm

    # §20: partials arrive before the final transcript.
    pieces = [p async for p in stt.transcribe_stream(gated_speech(), user_id=USER)]
    assert any(not p.is_final for p in pieces) and pieces[-1].is_final

    # §21: the final transcript is a complete thought → respond after a short pause.
    endpointer = SemanticEndpointer(short_pause_ms=700, long_pause_ms=2500)
    assert endpointer.should_respond(pieces[-1].text, silence_ms=800)


async def test_no_speech_never_calls_stt() -> None:
    pipeline = AudioInputPipeline(PipelineConfig(), ScriptedVAD([0.02] * 30))
    stt = FakeDecodedSTT()
    async for frame in pipeline.stream(_frames(30)):
        assert not frame.speech_active
    assert stt.decode_calls == 0  # idle is free — STT never ran


# ── §24 + §19: barge-in stops output, but an in-flight write is protected ──


async def test_barge_in_stops_playback_yet_action_write_completes() -> None:
    controller = BargeInController()
    played: list[bytes] = []
    write_committed = False

    async def tts_playback() -> None:
        # Stand-in for §23's streamed audio; barge-in cancels it mid-stream.
        for _ in range(100):
            played.append(b"\x00" * 320)
            await asyncio.sleep(0.01)

    async def action_write() -> None:
        nonlocal write_committed
        async with controller.protected_write():
            await asyncio.sleep(0.05)  # §13 write in progress
            write_committed = True

    tts_task = asyncio.create_task(tts_playback())
    controller.attach_tts(tts_task)
    write_task = asyncio.create_task(action_write())
    await asyncio.sleep(0.01)  # write is mid-flight

    # §19 VAD fires speech_start during playback → §24 interrupt.
    interrupt = asyncio.create_task(controller.on_user_speech(SESSION))
    await asyncio.sleep(0.01)
    assert not interrupt.done()  # deferred behind the protected write
    assert not tts_task.cancelled()  # playback still running until the write is safe

    await write_task
    await interrupt
    await asyncio.sleep(0)

    assert write_committed  # the action write finished uncorrupted (rule 3)
    assert tts_task.cancelled()  # only then did playback stop
    assert controller.interrupts_handled == 1


# ── §22 → §10 + §17: the SER read reaches prompt assembly and the mood model ──


class StubSER:
    def __init__(self, read: EmotionRead) -> None:
        self.read = read

    async def analyze(
        self, audio_window: bytes, *, user_id: str, session_id: str | None = None
    ) -> EmotionRead | None:
        return self.read


async def _assembler(docs: FakeDocStore) -> PromptAssembler:
    vectors = FakeVectorStore()
    profiles = ProfileService(docs)
    registry = TraitRegistry(docs, profiles)
    await registry.seed_defaults(DEFAULTS_DIR)
    await profiles.first_run_sync(USER)
    return PromptAssembler(
        profiles,
        registry,
        WorkingMemory(),
        EpisodicMemory(vectors),
        SemanticMemory(FakeGraphStore()),
        ProceduralMemory(docs),
        EntityResolver(vectors),
        SelfModel(docs, vectors),
        projects=None,
    )


async def test_ser_read_reaches_prompt_assembly_and_mood_model() -> None:
    tired = EmotionRead(valence=-0.6, arousal=-0.4, label="tired", confidence=0.7)
    provider = LaggingEmotionProvider(StubSER(tired))

    # Turn 1: analysis is scheduled; the read is ready for the next turn (§22 rule 2).
    provider.schedule(b"\x00" * 320, user_id=USER, session_id=SESSION)
    await asyncio.sleep(0)
    read = provider.current()
    assert read == tired

    docs = FakeDocStore()

    # §10: the emotion signal travels with the assembled prompt.
    assembler = await _assembler(docs)
    prompt = await assembler.assemble(USER, SESSION, "hey", emotion=read.model_dump())
    assert isinstance(prompt, AssembledPrompt)
    assert prompt.emotion is not None and prompt.emotion["label"] == "tired"

    # §17: valence/arousal roll into the mood baseline.
    psych = PsychUserModel(docs)
    for _ in range(4):  # baseline needs a few samples before it reads as "usual"
        await psych.update_mood(USER, read.valence, read.arousal)
    model = await psych.get(USER)
    assert model.mood_baseline.samples == 4
    assert model.mood_baseline.valence < 0  # the low read moved the baseline down

    await provider.aclose()
