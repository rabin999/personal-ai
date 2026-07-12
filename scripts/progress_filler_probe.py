"""Real-call proof for the slow-turn progress fillers (§8.12).

Drives a REAL live-info voice turn through the WIRED engine (real OpenRouter model + real
Mongo/Qdrant/Neo4j/Redis + real Serper search) — the same `orchestrator.generate_spoken`
the voice edge calls — and TIMESTAMPS every chunk handed to TTS. It proves that during the
dead air of a slow search the user hears, in order:

    1. the instant interjection   ("On it — let me check.")        [purpose=ack]
    2. progress line(s)            ("Still on it — almost there.")  [purpose=progress_ack]
    3. the streamed answer         (clause by clause)               [reply_chunk]

so the gap between (1) and (3) is filled rather than silent — keeping the user in the loop.

Run:  uv run python -m scripts.progress_filler_probe
"""

from __future__ import annotations

import asyncio
import time
import uuid

from config.settings import get_settings
from core.memory.working import Turn
from core.reasoning.prompt_assembly import DisambiguationRequest
from core.tools.registry import ToolContext
from tests.support.real_pipeline import RealTurns

# A genuinely-slow live-info question: a live lookup that must hit the network, so there is
# real dead air between the first ack and the first answer chunk for the fillers to fill.
QUESTION = "who is the current prime minister of nepal right now?"


async def main() -> None:
    settings = get_settings()
    print(
        f"progress_filler_gap_s={settings.progress_filler_gap_s}  "
        f"progress_filler_max={settings.progress_filler_max}\n"
    )
    turns = await RealTurns.build()
    p = turns.pipeline
    user = "u_demo_001"
    session = f"filler_probe_{uuid.uuid4().hex[:6]}"

    p.working.append(session, Turn(role="user", text=QUESTION))
    prompt = await p.assembler.assemble(user, session, QUESTION)
    if isinstance(prompt, DisambiguationRequest):
        print("unexpected disambiguation — pick a clearer question")
        return

    t0 = time.monotonic()
    timeline: list[tuple[float, str]] = []

    async def speak(text: str) -> None:
        if text.strip():
            timeline.append(((time.monotonic() - t0) * 1000, text.strip()))

    async def flush() -> None:
        # Voice flushes the current utterance so its audio plays now; here we only mark it.
        timeline.append(((time.monotonic() - t0) * 1000, "· flush ·"))

    ctx = ToolContext(user_id=user, session_id=session, project_id=None)
    with p.logs.bind(trace_id=session, turn_id=1, user_id=user):
        result = await p.orchestrator.generate_spoken(prompt, p.dispatcher, ctx, speak, flush=flush)
    total_ms = (time.monotonic() - t0) * 1000

    print(f"utterance: {QUESTION!r}\n")
    print("what the user HEARD, in order (ms from turn start):")
    first_answer_ms: float | None = None
    n_progress = 0
    for ms, text in timeline:
        tag = ""
        if text == "· flush ·":
            print(f"  {ms:8.0f}  {text}")
            continue
        # Classify against the fillers so the timeline is readable.
        from core.reasoning.response_gen import (
            _ACK_LOOKUP,
            _ACK_PROGRESS_APOLOGY,
            _ACK_PROGRESS_LOOKUP,
            _ACK_PROGRESS_THINKING,
            _ACK_THINKING,
        )

        if text in set(_ACK_LOOKUP) | set(_ACK_THINKING):
            tag = "  [ack]"
        elif text in set(_ACK_PROGRESS_APOLOGY):
            tag = "  [PROGRESS FILLER · apology]"
            n_progress += 1
        elif text in set(_ACK_PROGRESS_LOOKUP) | set(_ACK_PROGRESS_THINKING):
            tag = "  [PROGRESS FILLER]"
            n_progress += 1
        else:
            tag = "  [answer]"
            if first_answer_ms is None:
                first_answer_ms = ms
        print(f"  {ms:8.0f}  {text!r}{tag}")

    print(f"\ntotal turn: {total_ms:.0f} ms")
    if first_answer_ms is not None:
        print(f"first real answer chunk at: {first_answer_ms:.0f} ms")
    print(f"progress fillers emitted: {n_progress}")
    print(f"\nfinal reply: {result.voice_text or result.final_text!r}")

    # The durable trace confirms the spans independently of the spoken text.
    spans = await p.traces.traces_for(user, session)
    purposes = [
        (s.get("data") or {}).get("purpose")
        for s in spans
        if s.get("stage") == "llm" and (s.get("data") or {}).get("purpose")
    ]
    print(f"\nllm-span purposes (trace): {purposes}")
    assert "progress_ack" in purposes or n_progress == 0, (
        "a progress filler was spoken but no progress_ack span was traced"
    )


if __name__ == "__main__":
    asyncio.run(main())
