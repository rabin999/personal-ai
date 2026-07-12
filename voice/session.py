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
import random
import re
import time
import traceback
from collections import deque
from collections.abc import AsyncIterator
from typing import Any, Protocol

from core.audio.awareness import HealthCheckin, HealthMonitor
from core.errors import PROGRAMMING_ERRORS
from core.memory.compaction import SessionCompactor
from core.memory.conversation_store import ConversationStore
from core.memory.episodic import EpisodicMemory
from core.memory.extraction import MemoryExtractor
from core.memory.vocab import VocabProvider
from core.memory.working import Turn, WorkingMemory
from core.observability.logger import StructuredLogger
from core.reasoning.orchestrator import Orchestrator
from core.reasoning.prompt_assembly import AssembledPrompt, DisambiguationRequest, PromptAssembler
from core.reasoning.response_gen import GenerationResult, ToolDispatch
from core.tools.registry import ToolContext
from ports.stt import STT
from ports.tts import TTS, StreamingTTS
from voice.emotion import LaggingEmotionProvider
from voice.endpointing import SemanticEndpointer
from voice.multiutterance import classify_utterance, combine
from voice.pipeline import AudioInputPipeline, PipelineConfig, VADModel
from voice.sound import LaggingSoundProvider
from voice.trace import TraceEmitter

logger = logging.getLogger(__name__)

TTS_SAMPLE_RATE = 24_000  # §23 gpt-audio pcm16 output


class Delivery(Protocol):
    """Pull-at-pause background result delivery (§14)."""

    async def deliveries_for_pause(
        self, session_id: str, user_id: str, recent_context: str
    ) -> list[Any]: ...

    async def deliveries_at_open(self, user_id: str, session_id: str) -> list[Any]:
        """Results that finished while the user was away, carried to this open (U9)."""
        ...


SAMPLE_RATE = 16_000
_MS_PER_BYTE = 1000.0 / (SAMPLE_RATE * 2)  # PCM16 mono
# Poll for a finished background result about this often while idle (~1.3s at
# 32ms/frame). Cheap: only synthesizes when a task has actually completed (§8.2).
_DELIVERY_POLL_FRAMES = 40
# Rolling pre-roll ring buffer (spec §19 fix): the VAD gate only fires
# ``speech_start`` after START_FRAMES of consecutive speech, so the frames that
# opened the gate — plus the quiet onset before them — precede the event. Without
# a pre-roll the first phoneme/word is clipped. 10 frames ≈ 320ms at 512-sample
# (32ms) frames, comfortably covering the START_FRAMES(3) gate latency + onset.
_PREROLL_FRAMES = 10
# Barge-in needs *sustained* fresh speech, not the 3-frame gate onset: a brief
# residual-echo blip (even with browser AEC on) can flip 3 frames and falsely
# cancel the reply. ~8 frames ≈ 256ms — longer than an echo transient, shorter
# than a real interruption — so the companion stops for the user, not for itself.
_BARGE_IN_FRAMES = 8
# Two regimes so a false barge-in on ambient noise/breathing (while the user thinks
# silently) is filtered without losing a real interruption:
#  - CLEAR gated speech: if the window has at least this many is_speech frames (VAD
#    hysteresis cleared), a real interruption — commit at _BARGE_IN_FRAMES.
#  - RAW-ONLY energy over the lowered barge bar but below the is_speech gate: this is
#    EITHER AEC-attenuated double-talk speech OR noise — indistinguishable per-frame, so
#    require a longer sustain before committing (short noise blips never reach it).
_BARGE_IN_SPEECH_MIN = 4
_BARGE_IN_FRAMES_RAW = 12
# After this much unbroken silence — the user having ALREADY spoken this session, so
# it continues the conversation rather than initiating one (§3.6.4) — the companion
# gently checks in once ("still there? / lost in thought?"). Reset on the next speech.
_LULL_MS = 22_000.0

# A fresh ANGLE is drawn each open so the greeting prompt itself differs every
# session — the model was converging on the same "welcome back" line because the
# input never changed. Combined with a hotter temperature, this keeps hellos varied.
_GREETING_ANGLES = (
    "if you GENUINELY know their local time of day, you can nod to it naturally — but "
    "ONLY if the prompt actually tells you their local time; never guess morning/evening",
    "riff lightly on how long it's been since you last talked",
    "just an easy, plain 'hey' with their name — nothing extra",
    "be a little playful or teasing",
    "sound genuinely glad they showed up, warm but brief",
    "open low-key and breezy, like catching up mid-thought",
    "pick up on the vibe of the moment and keep it casual",
    "a short, curious 'hey, you' kind of energy",
)

# The user explicitly ending the conversation (spoken). Kept deliberately tight so a
# passing "bye the way" or "goodbye kiss" story doesn't hang up on them.
_FAREWELL = re.compile(
    r"\b(bye|byebye|goodbye|good bye|see (you|ya)( later| soon)?|talk (to you )?later|"
    r"catch you later|gotta go|got to go|i'?m done( talking)?|that'?s all( for now)?|"
    r"(close|end|stop) (the |this )?(conversation|chat|call)|hang up|let'?s stop)\b",
    re.IGNORECASE,
)


def _is_farewell(text: str) -> bool:
    return bool(_FAREWELL.search(text or ""))


# F3: when a dependency (LLM/STT/search/store) fails mid-turn the companion says so
# honestly and keeps the conversation alive — it never fabricates and never goes
# silent. In-voice, not a service-desk apology.
_DEPENDENCY_FAILURE_LINE = "Sorry — something on my end just dropped. Say that again?"


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
        # The turn engine behind the Orchestrator port (A1.5) — the edge is handed
        # `pipeline.orchestrator`, so it must be typed as the PORT, not one concrete
        # engine. Typing it as the engine hid a live TypeError behind duck-typing.
        generator: Orchestrator,
        tts: TTS,
        working: WorkingMemory,
        trace: TraceEmitter,
        episodic: EpisodicMemory | None = None,
        emotion: LaggingEmotionProvider | None = None,
        sound: LaggingSoundProvider | None = None,  # U10-U12 audio-awareness stage
        voice: str | None = None,
        barge_in: bool = True,
        dispatcher: "ToolDispatch | None" = None,
        delivery: "Delivery | None" = None,
        vocab: VocabProvider | None = None,
        conversations: ConversationStore | None = None,
        extractor: MemoryExtractor | None = None,
        defer_routing: bool = True,
        engine: str = "native",
        compactor: "SessionCompactor | None" = None,
        logs: StructuredLogger | None = None,
        evaluator: Any = None,
        greet_on_open: bool = True,  # speak a dynamic hello when the session opens (§3.6.3)
    ) -> None:
        self._user_id = user_id
        # §6/§7: per-turn LLM-as-judge (off the reply path) → scores on the same
        # (session, turn) Langfuse trace. Voice turns went unscored before (only the
        # text path scheduled it), so Langfuse showed no evaluator for voice.
        self._evaluator = evaluator
        # Bind correlation ids around each turn so the per-LLM-call spans (purpose,
        # prompt, params, tokens, cost) the LLM adapter logs persist to THIS turn's
        # trace — without it, voice traces showed only surface stage spans (C1 was
        # only wired on the text path). Stored below.
        self._logs = logs
        self._session_id = session_id
        self._pipeline = AudioInputPipeline(config, vad)
        # Barge-in detects speech during playback at a lower bar than turn-start
        # (§24 + browser-AEC double-talk attenuation) — see PipelineConfig.
        self._barge_threshold = config.barge_in_threshold
        # Monotonic time the CLIENT is expected to still be playing audio until.
        # The turn task finishes when the server has *sent* all audio, but the client
        # keeps playing that buffered audio for seconds — barge-in must stay armed for
        # the whole real playback, not just while the turn task runs (C2 fix: "it
        # keeps talking when I interrupt"). Updated per-chunk in ``converse``.
        self._playback_until = 0.0
        self._stt = stt
        self._endpointer = endpointer
        self._assembler = assembler
        self._generator = generator
        self._tts = tts
        self._working = working
        self._trace = trace
        self._episodic = episodic
        self._emotion = emotion
        self._sound = sound  # U10-U12 sound stage (one turn behind)
        self._health = HealthMonitor()  # U10 per-session cough tracker (no-nag)
        # §1.1: the companion never speaks first. A carried background result (U9) may
        # only be delivered once the user has spoken at least once THIS session — not
        # into the opening silence — so this gates the at-open carry.
        self._user_has_spoken = False
        self._voice = voice
        self._barge_in = barge_in
        self._dispatcher = dispatcher
        self._delivery = delivery
        self._vocab = vocab
        self._conversations = conversations
        self._extractor = extractor
        self._defer_routing = defer_routing
        self._engine = engine
        self._compactor = compactor
        self._turn_index = 0  # verbatim conversation-log turn counter (§6)
        self._vocab_terms: list[str] | None = None  # resolved once per session
        # Serialize background delivery: the idle poll and each turn both call
        # _deliver_pending; without this they can pull the same finished task
        # before either marks it delivered, the same result gets spoken 2-3x (§14).
        self._delivery_lock = asyncio.Lock()
        self._delivered_ids: set[str] = set()
        # U9: pull results carried over from a prior (closed) session exactly once,
        # at the first delivery check of this conversation open.
        self._carried_pulled = False
        # §3.6.4 silence-lull: check in at most once per lull; re-armed on next speech.
        self._lull_checked = False
        # §3.6.3: speak a dynamic hello the moment the session opens (config-gated).
        self._greet_on_open_enabled = greet_on_open
        # A4 multi-utterance: the previous endpointed (transcript, monotonic ms) and
        # whether the in-flight turn has begun speaking (→ an addition is a barge-in).
        self._prev_endpoint: tuple[str, float] | None = None
        self._turn_spoke = False
        self._capture_start_ms = 0.0  # monotonic ms when the current utterance began (A4 gap)
        self._greeting_task: asyncio.Task[None] | None = None  # the open greeting, discardable

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
                # Estimate how long the client will still be PLAYING: each 24kHz
                # PCM16 chunk is len/(24000*2) seconds of audio. Chunks drain to the
                # client faster than real time, so accumulate onto a running "playing
                # until" cursor — this keeps barge-in armed for the whole reply even
                # after the turn task has finished streaming (C2).
                dur = len(chunk) / (TTS_SAMPLE_RATE * 2)
                self._playback_until = max(self._playback_until, time.monotonic()) + dur
                yield chunk
            # F3: the None sentinel is written by _consume's `finally`, so the consumer
            # has finished. Await it so a programming error it raised SURFACES here —
            # the `gather(..., return_exceptions=True)` below would otherwise silently
            # discard it, which is exactly how a dead turn path went unnoticed.
            await consumer
        finally:
            if not consumer.done():
                consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    # ── continuous state machine ─────────────────────────────────────────

    async def _consume(
        self, frames: AsyncIterator[bytes], out: asyncio.Queue[bytes | None]
    ) -> None:
        # Record the pinned voice on the session span (spec §2b): one voice for the
        # whole session, visible in the trace so a voice change would be detectable.
        self._trace.emit(
            "session",
            "conversation started",
            user_id=self._user_id,
            voice=self._voice,
            engine=self._engine,  # §11: which voice runtime produced this session
        )
        buffer: list[bytes] = []
        # Pre-roll ring of the most recent pre-speech frames (§19 first-word fix).
        preroll: deque[bytes] = deque(maxlen=_PREROLL_FRAMES)
        silence_ms = 0.0
        barge_frames = 0
        barge_speech_frames = 0  # of the barge window, how many were GATED speech (not noise)
        idle_frames = 0
        lull_ms = 0.0  # unbroken idle silence, for the §3.6.4 lull check-in
        decided_incomplete = False
        capturing = False
        # STT re-use: skip transcribing the same audio twice when only silence was
        # added between the short- and long-pause endpoint checks (latency, §36).
        speech_since_transcribe = False
        last_transcript = ""
        turn: asyncio.Task[None] | None = None
        # Speak a warm, contextual hello the moment the session opens (§3.6.3) — but as a
        # SEPARATE cancellable task, not a blocking await. The frame loop keeps running, so
        # if the user is already talking (or starts the instant they connect) the greeting
        # is discarded the moment real speech begins (see the speech_start handler) — an
        # unsolicited greeting must never talk over them (user report), even when manual
        # barge-in is off for replies.
        if self._greet_on_open_enabled:
            self._greeting_task = asyncio.create_task(self._greet_on_open(out))
        try:
            async for frame in self._pipeline.stream(frames):
                frame_ms = len(frame.pcm) * _MS_PER_BYTE

                if turn is not None and turn.done():
                    # F3: never drop a finished turn task on the floor. A programming
                    # error inside it must propagate out of the conversation instead of
                    # being lost to "Task exception was never retrieved".
                    if not turn.cancelled() and (exc := turn.exception()) is not None:
                        raise exc
                    turn = None  # reply finished; back to listening

                # The OPEN greeting yields to the user unconditionally: the instant real
                # speech begins, discard the (unsolicited) greeting and listen — no barge-in
                # threshold, independent of the barge_in setting, so the companion never
                # talks over the user's opening words. Its audio still queued is flushed.
                if self._greeting_task is not None:
                    if self._greeting_task.done():
                        if (
                            not self._greeting_task.cancelled()
                            and (gexc := self._greeting_task.exception()) is not None
                        ):
                            self._greeting_task = None
                            raise gexc
                        self._greeting_task = None
                    elif frame.event == "speech_start" or frame.is_speech:
                        self._greeting_task.cancel()
                        await asyncio.gather(self._greeting_task, return_exceptions=True)
                        self._greeting_task = None
                        self._playback_until = 0.0  # greeting no longer playing → capture now
                        flushed = self._drain(out)
                        self._trace.emit(
                            "barge_in",
                            "user spoke over the open greeting — greeting discarded",
                            phase="greeting_discarded",
                            flushed_chunks=flushed,
                        )

                # Barge-in: user speaks while the companion is talking (§24). "Talking"
                # means EITHER a turn task is still generating/streaming OR the client
                # is still PLAYING already-sent audio (self._playback_until in the
                # future) — the reply keeps sounding for seconds after the turn task
                # finishes, and the user must be able to cut in during ALL of it (C2).
                # Requires sustained *fresh* raw speech (not a brief echo blip) so a
                # reply isn't self-interrupted.
                speaking = turn is not None or time.monotonic() < self._playback_until
                if speaking:
                    # Lower bar than turn-start: AEC has removed our own TTS, so a
                    # near-end signal over this threshold is the user — even when
                    # double-talk suppression has attenuated it below is_speech.
                    speaking_over = frame.confidence >= self._barge_threshold
                    if speaking_over:
                        barge_frames += 1
                        barge_speech_frames += 1 if frame.is_speech else 0
                    else:
                        barge_frames = barge_speech_frames = 0
                    # Commit on CLEAR speech quickly, but make raw-only energy (attenuated
                    # speech OR noise) sustain longer — so breathing/room noise while the
                    # user thinks silently no longer self-interrupts the reply, while genuine
                    # attenuated double-talk speech still cuts in (user report: false barge-ins).
                    clear_speech = barge_speech_frames >= _BARGE_IN_SPEECH_MIN
                    if (
                        self._barge_in
                        and barge_frames >= _BARGE_IN_FRAMES
                        and (clear_speech or barge_frames >= _BARGE_IN_FRAMES_RAW)
                    ):
                        # C2: real interruption — stop the OUTGOING AUDIO, not just
                        # generation. Order: (1) tell the client to flush its playback
                        # buffer immediately (barge_in event → client mutes + drops
                        # in-flight audio), (2) cancel the in-flight turn (its finally
                        # closes the TTS stream + cancels the audio pump), then (3) drain
                        # any already-synthesized chunks still queued here and stop the
                        # playback-tail clock so we don't re-trigger.
                        self._trace.emit(
                            "barge_in",
                            "user interrupted — stopping playback",
                            phase="detected",
                        )
                        if turn is not None:  # cancel in-flight generation + pending TTS
                            turn.cancel()
                            await asyncio.gather(turn, return_exceptions=True)
                            turn = None
                        self._playback_until = 0.0  # nothing should be playing now
                        flushed = self._drain(out)  # flush queued synthesized audio
                        self._trace.emit(
                            "barge_in",
                            f"playback stopped — TTS stream closed, {flushed} queued "
                            f"audio chunk(s) flushed, generation cancelled → listening",
                            phase="stopped",
                            flushed_chunks=flushed,
                        )
                        self._trace.begin_turn()
                        capturing, buffer, silence_ms = True, [frame.pcm], 0.0
                        barge_frames = barge_speech_frames = 0
                    continue  # replying: ignore our own trailing silence

                if frame.event == "speech_start":
                    self._trace.begin_turn()
                    # Seed the utterance with the pre-roll so the onset the gate
                    # swallowed (START_FRAMES + quiet lead-in) is transcribed too.
                    buffer = list(preroll)
                    self._trace.emit(
                        "vad", "speech detected — capturing", preroll_frames=len(buffer)
                    )
                    capturing, silence_ms, decided_incomplete = True, 0.0, False
                    speech_since_transcribe, last_transcript = True, ""  # fresh utterance
                    lull_ms, self._lull_checked = 0.0, False  # re-arm the lull check-in
                    # A4: when this utterance STARTED, so the multi-utterance gap is the
                    # SILENCE since the previous endpoint — not that silence plus however
                    # long this sentence took to say (a long continuation used to always
                    # exceed the gap and wrongly split into a second turn).
                    self._capture_start_ms = time.monotonic() * 1000
                if not capturing:
                    preroll.append(frame.pcm)  # keep the rolling pre-roll fresh
                    # §8.2: while idle, proactively deliver a finished background
                    # result at this pause (no user turn needed). Cheap poll —
                    # only synthesizes when something has actually completed.
                    idle_frames += 1
                    lull_ms += frame_ms
                    if idle_frames >= _DELIVERY_POLL_FRAMES and self._delivery is not None:
                        idle_frames = 0
                        turn = asyncio.create_task(self._deliver_pending(out))
                    # §3.6.4: after a long unbroken silence — the user having ALREADY
                    # spoken this session — gently check in ONCE (dynamic, never canned).
                    # Never while a turn/delivery is in flight, never before they've said
                    # anything (invariant: the companion never speaks first).
                    elif (
                        turn is None
                        and self._user_has_spoken
                        and not self._lull_checked
                        and lull_ms >= _LULL_MS
                    ):
                        self._lull_checked = True
                        turn = asyncio.create_task(self._lull_check_in(out))
                    continue  # §19 idle gate: nothing paid runs during silence
                idle_frames = 0
                lull_ms = 0.0  # the user is speaking again — reset the lull clock

                buffer.append(frame.pcm)
                if frame.is_speech:  # raw per-frame verdict, not the gate hysteresis
                    silence_ms = 0.0
                    speech_since_transcribe = True  # buffer's WORDS changed
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

                # Latency: only pay for STT when the SPEECH content changed. The
                # incomplete→long-pause re-check adds only silence, so its transcript is
                # identical — reuse it instead of transcribing the same audio twice.
                if speech_since_transcribe or not last_transcript:
                    # F3: STT runs HERE, outside _run_turn_inner's guard. An adapter
                    # outage must degrade (say so, keep listening); a bug must not hide.
                    stt_started = time.perf_counter()
                    try:
                        last_transcript = await self._transcribe(buffer)
                        # The STT gap the USER feels: end-of-speech has already elapsed
                        # (the endpointer's pause), and nothing downstream can start until
                        # this returns. Previously untimed — F6 needs it to be measured,
                        # not inferred.
                        self._trace.emit(
                            "stt",
                            "transcription complete",
                            duration_ms=round((time.perf_counter() - stt_started) * 1000, 1),
                            audio_ms=round(len(b"".join(buffer)) * _MS_PER_BYTE, 1),
                        )
                    except PROGRAMMING_ERRORS:
                        self._fail_loudly("speech-to-text", "the STT path")
                        raise
                    except Exception as exc:
                        self._degrade("speech-to-text", exc)
                        await self._say_step_failed(out)
                        capturing, buffer, silence_ms = False, [], 0.0
                        continue
                    speech_since_transcribe = False
                transcript = last_transcript
                if not transcript.strip():
                    capturing, buffer = False, []
                    continue
                decision = self._endpointer.decide(transcript, silence_ms)
                if not decision.respond:
                    decided_incomplete = True  # wait for the long pause instead
                    continue

                self._trace.emit(
                    "endpoint",
                    f"complete_thought={decision.complete_thought}",
                    complete=decision.complete_thought,
                    threshold_ms=decision.threshold_ms,
                )
                utterance = b"".join(buffer)
                capturing, buffer, silence_ms = False, [], 0.0
                # A4: is this a fresh turn, or a quick addition to the one just
                # said? If the previous turn hasn't started speaking yet and this
                # continues it, fold them into ONE turn instead of two.
                now_ms = time.monotonic() * 1000
                if self._prev_endpoint is not None:
                    prev_text, prev_ms = self._prev_endpoint
                    # The gap is the SILENCE between the previous endpoint and the moment
                    # this utterance began — not up to when it finished. Falls back to the
                    # endpoint time if onset wasn't recorded.
                    onset_ms = self._capture_start_ms or now_ms
                    dec = classify_utterance(
                        prev_text,
                        transcript,
                        onset_ms - prev_ms,
                        response_started=self._turn_spoke,
                    )
                    self._trace.emit(
                        "endpoint",
                        f"multi-utterance: {dec.decision}",
                        decision=dec.decision,
                        reason=dec.reason,
                        gap_ms=round(dec.gap_ms),
                    )
                    if dec.decision in ("accumulate", "merge"):
                        # Fold into ONE turn. The prior turn (if it hadn't yet
                        # spoken) is cancelled — by barge-in already, or here.
                        if turn is not None and not turn.done():
                            turn.cancel()
                            await asyncio.gather(turn, return_exceptions=True)
                            self._drain(out)
                        transcript = combine(prev_text, transcript, dec.decision)
                self._prev_endpoint = (transcript, now_ms)
                self._turn_spoke = False
                turn = asyncio.create_task(self._run_turn(transcript, utterance, out))
            # The frame stream ended (client stopped). Drain the in-flight turn with a
            # plain `await` so a programming error it raised propagates (F3) instead of
            # being discarded by the `return_exceptions=True` gather below.
            if turn is not None:
                pending, turn = turn, None
                await pending
            # A silent session that was never interrupted: let the open greeting finish so
            # it's actually spoken + logged (a programming error in it still propagates, F3).
            if self._greeting_task is not None:
                greeting, self._greeting_task = self._greeting_task, None
                await greeting
        except asyncio.CancelledError:
            if turn is not None:
                turn.cancel()
            if self._greeting_task is not None:
                self._greeting_task.cancel()
            raise
        finally:
            if turn is not None:
                await asyncio.gather(turn, return_exceptions=True)
            if self._greeting_task is not None:
                await asyncio.gather(self._greeting_task, return_exceptions=True)
            out.put_nowait(None)

    def _drain(self, out: asyncio.Queue[bytes | None]) -> int:
        """Flush all already-synthesized audio chunks still queued for the client
        (C2). Returns how many were dropped so the trace can show the flush firing."""
        dropped = 0
        while not out.empty():
            try:
                out.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        return dropped

    # ── one turn (utterance already endpointed) ──────────────────────────

    async def _run_turn(
        self, transcript: str, utterance: bytes, out: asyncio.Queue[bytes | None]
    ) -> None:
        """Bind this turn's correlation ids (session, turn, user) so EVERY paid
        LLM call the adapter makes during generation logs a per-call span into THIS
        turn's trace (purpose, prompt, params, tokens, cost) — the deep trace, not
        just surface stage spans. Then run the turn."""
        if self._logs is None:
            await self._run_turn_inner(transcript, utterance, out)
            return
        with self._logs.bind(
            trace_id=self._session_id,
            turn_id=self._trace.current_turn,
            user_id=self._user_id,
        ):
            await self._run_turn_inner(transcript, utterance, out)

    async def _run_turn_inner(
        self, transcript: str, utterance: bytes, out: asyncio.Queue[bytes | None]
    ) -> None:
        try:
            self._trace.emit(
                "stt",
                f"final: {transcript!r}",
                text=transcript,
                engine=getattr(self._stt, "name", "stt"),  # §20: which STT produced this
            )

            # The user has now spoken this session — only after this may a carried
            # background result be delivered (§1.1 never-speak-first: delivering a
            # requested result at a pause is fine, but not into the opening silence).
            self._user_has_spoken = True
            emotion = self._emotion_signal()
            if self._emotion is not None:
                self._emotion.schedule(
                    utterance, user_id=self._user_id, session_id=self._session_id
                )
            # U10-U12: read last turn's sound classification, then schedule this
            # utterance's (one turn behind, like SER). Health check-in is debounced.
            sound = self._sound.current() if self._sound is not None else None
            health: HealthCheckin | None = None
            if sound is not None:
                recent = " ".join(t.text for t in self._working.recent(self._session_id, n=3))
                health = self._health.observe(sound, context_hint=recent)
                if health.should_check_in:
                    self._trace.emit("audio", "health check-in", sound=health.sound)
            if self._sound is not None:
                self._sound.schedule(utterance, user_id=self._user_id, session_id=self._session_id)

            # Pull-at-pause (§14): deliver any background result that finished
            # since last turn (e.g. a web search), in-voice, before the reply.
            await self._deliver_pending(out)

            self._working.append(self._session_id, Turn(role="user", text=transcript))
            prompt = await self._assembler.assemble(
                self._user_id,
                self._session_id,
                transcript,
                emotion=emotion,
                sound=sound,
                health=health,
            )
            context = self._tool_context(prompt)
            if isinstance(prompt, DisambiguationRequest):
                self._trace.emit(
                    "assembly",
                    "ambiguous reference — asking to disambiguate",
                    candidates=[c.name for c in prompt.candidates[:3]],
                )
            else:
                self._trace.emit(
                    "assembly",
                    f"prompt assembled (complexity={prompt.complexity_hint})",
                    complexity=prompt.complexity_hint,
                    entities=[c.name for c in prompt.resolved_entities],
                    prompt_version=prompt.prompt_version,
                    # F7: the real assembled prompt verbatim + trait/recall evidence,
                    # so a voice turn is evaluable from its trace alone (parity w/ chat).
                    system_prompt=prompt.system_prompt,
                    messages=prompt.messages,
                    active_traits=[f"{t['id']}:v{t['version']}" for t in prompt.active_traits],
                    trait_text=prompt.sections.get("traits", ""),
                    recall_source=prompt.recall_source,
                )
                self._trace.emit("router", f"routing to {prompt.complexity_hint} tier")

            # Stream the reply into TTS sentence-by-sentence (§8.12): the first
            # sentence starts synthesizing while the rest is still generating, so
            # the user hears audio far sooner. When the adapter supports it, the
            # whole turn feeds ONE WebSocket synthesis session so the voice stays
            # consistent across sentences (§2b/§23) — separate per-sentence REST
            # requests drift in tone. Falls back to per-call REST synthesis.
            self._trace.emit("tts", "synthesizing reply audio", voice=self._voice)
            result = await self._speak_turn(prompt, context, out)
            # No-silence guarantee: if a step returned an EMPTY reply without raising (so nothing
            # was streamed to TTS), never leave the user hanging — speak an honest short line.
            # (Exceptions are already covered by the except below; this covers the quiet path.)
            if not (result.voice_text or result.final_text).strip():
                self._trace.emit("response", "", level="warn", empty_reply=True)
                await self._say_step_failed(out)
            self._trace.emit(
                "generation",
                f"action={result.action}",
                action=result.action,
                turn_id=result.turn_id,
            )
            if result.style_flags:  # §7: tone regression is visible, not silent
                self._trace.emit(
                    "generation",
                    f"style warning: forbidden assistant-speak {result.style_flags}",
                    level="warn",
                    style_flags=result.style_flags,
                )
            # The trace keeps the raw tagged voice text; working memory + durable
            # stores get the clean, tag-free text (brief §1.4/§5.10).
            voice_text = result.voice_text or result.final_text
            self._trace.emit("response", result.final_text, voice_text=voice_text)
            self._working.append(self._session_id, Turn(role="assistant", text=result.final_text))
            self._remember(transcript, result.final_text)
            self._log_conversation(transcript, result.final_text, emotion)
            self._compact_if_needed()  # F14: bound the buffer over a long session
            # §6/§7: score this turn with the companion-voice judge, off the reply
            # path, onto the SAME (session, turn) Langfuse trace (no-op unless enabled).
            if self._evaluator is not None:
                self._evaluator.schedule(
                    session_id=self._session_id,
                    turn=self._trace.current_turn,
                    user_msg=transcript,
                    reply=result.final_text,
                )
            self._trace.emit("tts", "reply audio complete")
            # User asked to end ("bye", "close the conversation") → after the
            # companion's goodbye plays, signal the client to end + reset to Start.
            if _is_farewell(transcript):
                self._trace.emit("session", "user ended the conversation", end_conversation=True)
        except asyncio.CancelledError:
            self._trace.emit("barge_in", "reply cancelled", phase="cancelled")
            raise
        except PROGRAMMING_ERRORS:
            # F3: a defect in OUR code (wrong signature, missing attribute, broken
            # internal contract). Absorbing it is what made the companion answer every
            # turn with silence. Fail loudly and let it propagate.
            self._fail_loudly("voice turn", "the turn path")
            raise
        except Exception as exc:  # a dependency failed — degrade, don't die
            self._degrade("voice turn", exc)
            await self._say_step_failed(out)

    def _fail_loudly(self, what: str, where: str) -> None:
        """A bug in our own code: full traceback to the structured logger AND a failed
        step in the trace, so it can never be silent again (F3)."""
        logger.exception("%s failed with a PROGRAMMING ERROR in %s", what, where)
        detail = traceback.format_exc()
        if self._logs is not None:
            self._logs.log("error", "programming_error", stage="error", what=what, traceback=detail)
        self._trace.emit(
            "error",
            f"BUG in {where}: {detail.strip().splitlines()[-1]}",
            level="error",
            programming_error=True,
            traceback=detail,
        )

    def _degrade(self, what: str, exc: BaseException) -> None:
        """An external dependency failed: log with traceback, mark the step failed in
        the trace, and let the conversation continue (F3)."""
        logger.exception("%s failed on a dependency", what)
        if self._logs is not None:
            self._logs.log(
                "warn",
                "dependency_failure",
                stage="error",
                what=what,
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
            )
        self._trace.emit(
            "error",
            f"{type(exc).__name__}: {exc}",
            level="error",
            programming_error=False,
            degraded=True,
        )

    async def _say_step_failed(self, out: asyncio.Queue[bytes | None]) -> None:
        """Tell the user honestly that this turn failed, in-voice (§16: never fabricate,
        never go silent). Best-effort — if TTS is itself the failure, stay quiet."""
        try:
            await self._synthesize(_DEPENDENCY_FAILURE_LINE, out)
        except Exception:  # TTS itself is down; silence is all that's left
            logger.warning("could not speak the degraded-turn line", exc_info=True)

    async def _transcribe(self, speech: list[bytes]) -> str:
        async def _frames() -> AsyncIterator[bytes]:
            for pcm in speech:
                yield pcm

        vocab = await self._vocab_for_user()
        final_text = ""
        async for piece in self._stt.transcribe_stream(
            _frames(), vocab, user_id=self._user_id, session_id=self._session_id
        ):
            if piece.is_final:
                final_text = piece.text
        return final_text

    async def _vocab_for_user(self) -> list[str] | None:
        """The user's names/terms for STT boosting (§20 rule 2); fetched once, cached."""
        if self._vocab is None:
            return None
        if self._vocab_terms is None:
            self._vocab_terms = await self._vocab.terms_for(self._user_id)
            if self._vocab_terms:
                self._trace.emit(
                    "stt",
                    f"vocab boost: {len(self._vocab_terms)} user terms",
                    terms=self._vocab_terms[:10],
                )
        return self._vocab_terms or None

    def _emotion_signal(self) -> dict[str, float | str] | None:
        if self._emotion is None:
            return None
        read = self._emotion.current()
        if read is None:
            return None
        self._trace.emit("emotion", f"acoustic read: {read.label}", **read.model_dump())
        return read.model_dump()

    async def _speak_turn(
        self,
        prompt: AssembledPrompt | DisambiguationRequest,
        context: ToolContext,
        out: asyncio.Queue[bytes | None],
        *,
        temperature: float | None = None,
    ) -> GenerationResult:
        """Generate + speak one turn, routing ALL of the turn's speech through a
        single TTS session so the voice never changes mid-reply (§2b). Prefers the
        streaming WebSocket session; degrades to per-sentence REST if the adapter
        can't stream or the session fails to open. Barge-in/turn-end always closes
        the session (finally) so synthesis stops and cost is logged (rule 5).
        ``temperature`` overrides the reply temperature (greetings run hotter so
        they vary session to session)."""
        stream = None
        pump: asyncio.Task[None] | None = None
        if isinstance(self._tts, StreamingTTS):
            try:
                stream = await self._tts.open_stream(
                    self._voice, user_id=self._user_id, session_id=self._session_id
                )
            except PROGRAMMING_ERRORS:
                raise  # F3: a wiring bug in the TTS adapter, not a flaky handshake
            except Exception:  # network/handshake — never fail the turn on TTS setup
                logger.warning("tts stream open failed; using per-call synthesis", exc_info=True)
                stream = None

        try:
            if stream is not None:
                s = stream  # bind for the closures/type-narrowing

                async def _pump() -> None:
                    async for chunk in s.audio():
                        out.put_nowait(chunk)

                pump = asyncio.create_task(_pump())

                async def speak(text: str) -> None:
                    self._turn_spoke = True  # A4: once speaking, a new utterance is a barge-in
                    await s.feed(text)
            else:

                async def speak(text: str) -> None:
                    self._turn_spoke = True
                    async for chunk in self._tts.speak(
                        text, self._voice, user_id=self._user_id, session_id=self._session_id
                    ):
                        out.put_nowait(chunk)

            result = await self._generator.generate_spoken(
                prompt, self._dispatcher, context, speak, temperature=temperature
            )
            if stream is not None:
                await stream.finish()  # flush the tail
                if pump is not None:
                    await pump  # drain remaining audio before the turn completes
            return result
        finally:
            if stream is not None:
                await stream.aclose()
            if pump is not None and not pump.done():
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)

    async def _greet_on_open(self, out: asyncio.Queue[bytes | None]) -> None:
        """Speak a warm, contextual hello when the conversation opens (§3.6.3). Built
        through the normal pipeline so it uses the user's name, persona, time-of-day,
        and how long it's been — then synthesized to audio. Best-effort: any failure is
        swallowed so a greeting hiccup never blocks the conversation."""
        try:
            note = await self._last_seen_note()
            avoid = await self._recent_greetings()
            angle = random.choice(_GREETING_ANGLES)
            instr = (
                "[The user just opened the app to talk with you. Greet them first, warmly and "
                "CASUALLY, in ONE short natural spoken line — like a friend genuinely glad they "
                f"showed up.{note} You don't need to use their name. For THIS greeting: {angle}. "
                "Make it FRESH and clearly DIFFERENT from a stock 'welcome back' — never reuse "
                "the same wording twice; vary the opener, rhythm and words every single time. "
                "Keep it informal. Do NOT ask 'how can I help' or end on a stock filler question "
                "like 'what's on your mind?' / 'what's up?'; just an easy, original warm hello."
                + (f" Do NOT repeat any of these recent openers: {avoid}]" if avoid else "]")
            )
            prompt = await self._assembler.assemble(self._user_id, self._session_id, instr)
            if isinstance(prompt, DisambiguationRequest):
                return
            # Route through the streaming turn path so the greeting is chunked to TTS
            # (first words start immediately) instead of synthesized whole (L4/§8.12).
            # Hotter temperature than a normal reply so the hello genuinely varies.
            result = await self._speak_turn(
                prompt, self._tool_context(prompt), out, temperature=0.9
            )
            text = (result.voice_text or result.final_text).strip()
            if not text:
                return
            self._trace.emit("response", text, voice_text=text, greeting=True)
            self._working.append(self._session_id, Turn(role="assistant", text=text))
            self._log_conversation("", text, None)  # the greeting is part of the log (§6)
        except PROGRAMMING_ERRORS:
            self._fail_loudly("open greeting", "the greeting path")  # F3: never silent
            raise
        except Exception as exc:  # a dependency hiccup must never break the session
            self._degrade("open greeting", exc)

    async def _lull_check_in(self, out: asyncio.Queue[bytes | None]) -> None:
        """After a long silence, gently check in ONCE — dynamic, in-voice, streamed to
        TTS (§3.6.4). Only ever called once the user has spoken, so it continues the
        conversation rather than initiating one. Best-effort; a hiccup never breaks the
        session, and the reply is logged/remembered like any other companion turn."""
        try:
            recent = " ".join(t.text for t in self._working.recent(self._session_id, n=4))
            instr = (
                "[There's been a stretch of silence — the user went quiet mid-conversation. "
                "Gently check in with ONE short, warm, natural spoken line: are they still "
                "there, did they get pulled away, are they lost in thought? Make it fresh "
                "and specific to where the chat left off, not a stock phrase. Don't restate "
                "what you were saying; just a light, caring nudge."
                + (f" The conversation so far: {recent}]" if recent.strip() else "]")
            )
            prompt = await self._assembler.assemble(self._user_id, self._session_id, instr)
            if isinstance(prompt, DisambiguationRequest):
                return
            result = await self._speak_turn(
                prompt, self._tool_context(prompt), out, temperature=1.0
            )
            text = (result.voice_text or result.final_text).strip()
            if not text:
                return
            self._trace.emit("response", text, voice_text=text, lull_check_in=True)
            self._working.append(self._session_id, Turn(role="assistant", text=text))
            self._log_conversation("", text, None)
        except PROGRAMMING_ERRORS:
            self._fail_loudly("lull check-in", "the check-in path")  # F3: never silent
            raise
        except Exception as exc:  # a dependency hiccup must never break the session
            self._degrade("lull check-in", exc)

    async def _last_seen_note(self) -> str:
        """A short note on how long since the user's last conversation, for the greeting
        ('that was quick' vs 'been a while'). Empty on the first-ever conversation or if
        the store is unavailable."""
        if self._conversations is None:
            return ""
        try:
            rows, _ = await self._conversations.list_conversations(self._user_id, limit=5)
        except Exception:
            return ""
        prior = [r for r in rows if r.get("session_id") != self._session_id]
        if not prior:
            return " This looks like their first time here."
        last = prior[0]
        stamp = last.get("last_ts") or last.get("last_at_ts")
        if not isinstance(stamp, int | float):
            return ""
        gap_s = max(0.0, time.time() - float(stamp))
        if gap_s < 1800:
            return " They were just here minutes ago — back quick."
        if gap_s < 6 * 3600:
            return " They last talked a few hours ago."
        if gap_s < 36 * 3600:
            return " They last talked earlier / yesterday."
        days = int(gap_s // 86400)
        return f" It's been about {days} day(s) since they last talked."

    async def _recent_greetings(self, n: int = 3) -> str:
        """The last few lines the companion SAID unprompted (greetings/check-ins),
        so the model can avoid repeating an opener verbatim — the user dislikes
        hearing the same hello every session. Empty if none / store unavailable."""
        if self._conversations is None:
            return ""
        try:
            rows = await self._conversations.recent_raw_turns(self._user_id, limit=8)
        except Exception:
            return ""
        lines: list[str] = []
        for r in rows:
            if str(r.get("user_text") or "").strip():
                continue  # a real exchange, not an unprompted opener
            text = str(r.get("assistant_text") or "").strip()
            if text:
                lines.append(text[:70])
            if len(lines) >= n:
                break
        return " | ".join(f'"{ln}"' for ln in lines)

    async def _synthesize(self, text: str, out: asyncio.Queue[bytes | None]) -> None:
        self._trace.emit("tts", "synthesizing reply audio", voice=self._voice)
        total = 0
        async for chunk in self._tts.speak(
            text, self._voice, user_id=self._user_id, session_id=self._session_id
        ):
            total += len(chunk)
            out.put_nowait(chunk)
        self._trace.emit("tts", f"reply audio complete ({total} bytes)", bytes=total)

    def _tool_context(self, prompt: AssembledPrompt | DisambiguationRequest) -> ToolContext:
        project_id = None
        if isinstance(prompt, AssembledPrompt):
            for c in prompt.resolved_entities:
                if c.entity_type == "project":
                    project_id = c.entity_id
                    break
        return ToolContext(
            user_id=self._user_id, session_id=self._session_id, project_id=project_id
        )

    async def _deliver_pending(self, out: asyncio.Queue[bytes | None]) -> None:
        """Speak any finished background result at this pause, in-voice (§14/§8.6).

        Serialized: the idle poll and the per-turn call can't both pull the same
        task before it's marked delivered, and an id guard blocks any re-delivery.
        """
        if self._delivery is None:
            return
        async with self._delivery_lock:
            recent = " ".join(t.text for t in self._working.recent(self._session_id, n=4))
            try:
                deliveries = await self._delivery.deliveries_for_pause(
                    self._session_id, self._user_id, recent
                )
                # U9: once per open — but only AFTER the user has spoken this session
                # (§1.1 never-speak-first) — surface results that finished while the
                # user was away in a prior session (stale ones are dropped inside).
                if self._user_has_spoken and not self._carried_pulled:
                    self._carried_pulled = True
                    carried = await self._delivery.deliveries_at_open(
                        self._user_id, self._session_id
                    )
                    deliveries = [*carried, *deliveries]
            except PROGRAMMING_ERRORS:
                self._fail_loudly("background delivery", "the delivery path")  # F3
                raise
            except Exception as exc:  # delivery is best-effort; never break the turn
                self._degrade("background delivery", exc)
                return
            for item in deliveries:
                line = getattr(item, "line", "")
                task_id = getattr(item, "task_id", "")
                if not line or (task_id and task_id in self._delivered_ids):
                    continue
                if task_id:
                    self._delivered_ids.add(task_id)
                self._trace.emit("response", line, delivered=True, task_id=task_id)
                # Remember what we told them (the news/search result): into working
                # memory (same-session recall) AND the durable conversation log, so a
                # follow-up like "tell me more about that" has context instead of
                # "what news?". Everything the companion says is stored (§6).
                self._working.append(self._session_id, Turn(role="assistant", text=line))
                if self._conversations is not None:
                    self._turn_index += 1
                    t = asyncio.create_task(
                        self._conversations.record_turn(
                            user_id=self._user_id,
                            session_id=self._session_id,
                            turn_index=self._turn_index,
                            user_text="",
                            assistant_text=line,
                            trace_turn=self._trace.current_turn,
                        )
                    )
                    t.add_done_callback(lambda t: t.exception())
                await self._synthesize(line, out)

    def _remember(self, user_text: str, assistant_text: str) -> None:
        """WRITE step (§1): decide what to persist (episodic / semantic / trades).

        Deferred by default (Item 9): the raw log is written by ``_log_conversation``
        and the background worker routes it via the cursor — nothing extra runs on
        the live path. Only the legacy inline path runs when routing isn't deferred."""
        if self._defer_routing:
            return  # routing is the worker's job (raw log already persisted)
        if self._extractor is not None:
            task = asyncio.create_task(self._extract(user_text, assistant_text))
            task.add_done_callback(lambda t: t.exception())  # swallow; best-effort
        elif self._episodic is not None:  # fallback if no extractor wired
            chunk = f"user: {user_text}\nassistant: {assistant_text}"
            task = asyncio.create_task(
                self._episodic.write(self._user_id, self._session_id, [chunk])
            )
            task.add_done_callback(lambda t: t.exception())

    async def _extract(self, user_text: str, assistant_text: str) -> None:
        assert self._extractor is not None
        extracted = await self._extractor.extract_and_store(
            self._user_id, self._session_id, user_text, assistant_text
        )
        if extracted.episodic_written or extracted.semantic_written or extracted.trades_written:
            self._trace.emit(
                "memory",
                f"stored {extracted.episodic_written} event(s), "
                f"{extracted.semantic_written} fact(s), {extracted.trades_written} trade(s)",
                semantic=extracted.facts,
                episodic=extracted.events,
            )

    def _compact_if_needed(self) -> None:
        """F14: fold older turns into the rolling summary when the session buffer
        grows long — off the reply path, best-effort, so the prompt stays bounded."""
        if self._compactor is None or not self._compactor.should_compact(self._session_id):
            return
        task = asyncio.create_task(self._compactor.maybe_compact(self._session_id, self._user_id))
        task.add_done_callback(lambda t: t.exception())

    def _log_conversation(
        self, user_text: str, assistant_text: str, emotion: dict[str, Any] | None
    ) -> None:
        """Append the raw exchange to the durable conversation log (§6); best-effort."""
        if self._conversations is None:
            return
        self._turn_index += 1
        task = asyncio.create_task(
            self._conversations.record_turn(
                user_id=self._user_id,
                session_id=self._session_id,
                turn_index=self._turn_index,
                user_text=user_text,
                assistant_text=assistant_text,
                trace_turn=self._trace.current_turn,
                emotion=emotion,
            )
        )
        task.add_done_callback(lambda t: t.exception())  # swallow; write is best-effort
