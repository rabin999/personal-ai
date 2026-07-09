"""Judged response-quality golden set, through the LIVE voice path (F4/F6).

Every scenario is spoken at the companion through ``VoiceSession.converse`` — real VAD,
real endpointing, real STT, the wired Orchestrator, real TTS — and the REAL reply is judged.

Why this exists: `tests/golden/test_gs3_judge.py` scores CANNED reply strings from
`gs3_judge.json`, so it can never catch a regression in what the engine actually says. It
reported "judge 1.0 PASS" while every live voice turn was raising TypeError and producing
silence. Nothing in `tests/` drove `VoiceSession`. This does.

Two gates:
  1. QUALITY — the calibrated companion-voice judge (core/eval/judge.py, the same rubric the
     live evaluator uses) + deterministic banned-assistant-speak checks on the SPOKEN text.
  2. DYNAMIC TONE — the same utterance under different emotional reads must produce a
     measurably DIFFERENT delivery. A uniformly "warm" reply is itself a failure.

NOTE on emotion: `settings.ser_service_url` is empty, so production currently has NO acoustic
read and every turn is register "neutral". Scenarios that pin an emotion inject it through the
SER port (see scripts/live_turn.FixedEmotionProvider) — that exercises the prosody mechanism,
but it is NOT what production does today. Reported explicitly.

Run:  PYTHONPATH=. uv run python -m scripts.quality_eval --label baseline_live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from pathlib import Path
from typing import Any

from api.composition import build_pipeline
from config.settings import get_settings
from core.eval.judge import judge_companion_voice
from core.reasoning.prosody import read_register
from core.reasoning.style import find_forbidden
from scripts.live_turn import drive_turn

USER = "u_demo_001"
OUT_DIR = Path("docs/quality")

# The reply temperature is 0.7, so one sample is noisy; a before/after gate on a single run
# would flag sampling noise as a regression.
REPEATS = 2

_SAD = {"label": "sad", "valence": -0.6, "arousal": 0.15, "confidence": 0.9}
_EXCITED = {"label": "excited", "valence": 0.7, "arousal": 0.8, "confidence": 0.9}
_STRESSED = {"label": "frustrated", "valence": -0.5, "arousal": 0.7, "confidence": 0.9}


class Scenario:
    def __init__(
        self,
        sid: str,
        utterance: str,
        *,
        emotion: dict[str, Any] | None = None,
        history: list[tuple[str, str]] | None = None,
        note: str = "",
    ) -> None:
        self.id = sid
        self.utterance = utterance
        self.emotion = emotion
        self.history = history or []
        self.note = note


SCENARIOS = [
    Scenario("trivial_greeting", "hi", note="must stay warm, not a stock filler question"),
    Scenario("thanks_ack", "thanks, that actually helps"),
    Scenario("memory_recall", "when do I take my meds?", note="must recall the real fact"),
    Scenario(
        "live_search",
        "what's the current LTP of OP?",
        note="KNOWN BROKEN — see docs/NEXT_CORRECTNESS_TASK.md. Recorded, not gated.",
    ),
    Scenario(
        "multi_intent",
        "what's the weather in Kathmandu, remind me of my dentist at 4, and what's OP trading at?",
    ),
    Scenario(
        "nature_disclosure",
        "do you actually care about me?",
        note="one honest warm sentence; never cold, never a canned disclaimer",
    ),
    Scenario(
        "indirect_intent",
        "what's happening in Nepal really gives me pain",
        note="must infer intent; never 'what do you mean?'",
    ),
    Scenario(
        "emotional_sad",
        "I've been feeling really low since my dad passed",
        emotion=_SAD,
        note="register down — gentle, no levity",
    ),
    Scenario(
        "emotional_excited", "I got the promotion!!", emotion=_EXCITED, note="register excited"
    ),
    Scenario(
        "blunt_frustrated",
        "just answer the question. why is this taking so long?",
        emotion=_STRESSED,
        note="register stressed — calm and direct, NOT gushing warmth",
    ),
    Scenario(
        "followup_reference",
        "is that going to be a problem?",
        history=[
            ("user", "my landlord raised the rent by 20%"),
            ("assistant", "Ouch, twenty percent is a real jump. How are you feeling about it?"),
        ],
        note="anaphora — 'that' must resolve",
    ),
]

TONE_PROBE = "I'm not sure what to do about work"
TONE_VARIANTS = [("sad", _SAD), ("excited", _EXCITED), ("neutral", None)]

_TONE_JUDGE = """You judge DELIVERY REGISTER, not content.

A voice companion replied to a user whose emotional state was read as: {register}.
Expected delivery:
- down: warm, gentle, quietly encouraging, unhurried. NO levity, NO cheeriness.
- stressed: calm, steady, grounding, direct. NOT bubbly, NOT gushing warmth.
- excited: upbeat, matches their energy, a little playful.
- upbeat: light and warm.
- neutral: natural conversational warmth, nothing forced.

5 = perfectly matched. 1 = actively mismatched (cheery on a grieving turn, gushing on a
blunt/frustrated turn, flat on an excited turn). A generically "warm" reply that ignores the
register scores 2.

Return ONLY JSON: {{"tone_fit": 1-5, "reason": "<one sentence>"}}"""


async def _tone_fit(llm: Any, register: str, user: str, reply: str) -> dict[str, Any]:
    if not reply.strip():
        return {"tone_fit": 1, "reason": "empty reply — the user heard nothing"}
    res = await llm.complete(
        "tone_judge",
        [
            {"role": "system", "content": _TONE_JUDGE.format(register=register)},
            {"role": "user", "content": f"USER said: {user!r}\nCOMPANION replied: {reply!r}"},
        ],
        "complex",
        response_format={"type": "json_object"},
        temperature=0.2,
        purpose="judge_tone",
    )
    text = res.text.strip()
    out: dict[str, Any] = json.loads(text[text.index("{") : text.rindex("}") + 1])
    return out


async def _run(pipeline: Any, sc: Scenario, run: int) -> dict[str, Any]:
    cap = await drive_turn(pipeline, USER, sc.utterance, emotion=sc.emotion, history=sc.history)
    return {
        "scenario": sc.id,
        "run": run,
        "utterance": sc.utterance,
        "transcript": cap.transcript,
        "register": read_register(sc.emotion),
        "reply": cap.reply_text,
        "action": cap.action,
        "style_flags": cap.style_flags,
        # Hard rule: banned assistant-speak absent from what the user HEARS. A nature
        # question legitimately permits the warm one-line disclosure (§1.2 rule 4), so it
        # must be measured with `allow_disclosure` — otherwise the harness flags the reply
        # the design ASKS for. (The engine's own `style_flags` already gets this right.)
        "banned_in_reply": find_forbidden(
            cap.reply_text, allow_disclosure=sc.id == "nature_disclosure"
        ),
        "empty_reply": not cap.reply_text.strip(),
        "audio_chunks": cap.audio_chunks,
        "first_audio_ms": cap.first_audio_ms,
        "total_ms": cap.total_ms,
        "llm_calls": len(cap.llm_calls),
        "purposes": cap.purposes,
        "ran_context_intent": cap.ran_context_intent,
        "searches": len(cap.searches),
        "discarded_drafts": cap.discarded_drafts,
        "cache_hits": cap.cache_hits,
        "cost_usd": cap.cost_usd,
        "graph_nodes": cap.graph_nodes,
        "exceptions": [e["type"] for e in cap.exceptions],
    }


async def _judge_all(pipeline: Any, records: list[dict[str, Any]]) -> None:
    for rec in records:
        reply = rec["reply"]
        if not reply.strip():
            rec["judge"] = {
                "companion_score": 1,
                "chatbot_like": False,
                "reason": "empty reply",
                "ok": False,
            }
        else:
            v = await judge_companion_voice(pipeline.llm, rec["utterance"], reply)
            rec["judge"] = {
                "companion_score": v.companion_score,
                "chatbot_like": v.chatbot_like,
                "reason": v.reason,
                "ok": v.ok,
            }
        rec["tone"] = await _tone_fit(pipeline.llm, rec["register"], rec["utterance"], reply)


async def _tone_gate(pipeline: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"probe": TONE_PROBE, "variants": []}
    for name, emo in TONE_VARIANTS:
        cap = await drive_turn(pipeline, USER, TONE_PROBE, emotion=emo)
        reg = read_register(emo)
        tone = await _tone_fit(pipeline.llm, reg, TONE_PROBE, cap.reply_text)
        out["variants"].append(
            {
                "emotion": name,
                "register": reg,
                "reply": cap.reply_text,
                "tone_fit": tone.get("tone_fit"),
                "reason": tone.get("reason"),
            }
        )
    replies = [v["reply"].strip().lower() for v in out["variants"]]
    out["all_distinct"] = len(set(replies)) == len(replies)
    fits = [v["tone_fit"] for v in out["variants"] if isinstance(v["tone_fit"], int | float)]
    out["min_tone_fit"] = min(fits) if fits else None
    return out


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by.setdefault(r["scenario"], []).append(r)
    summary: dict[str, Any] = {}
    for sid, runs in by.items():
        scores = [r["judge"]["companion_score"] for r in runs if "judge" in r]
        tones = [r["tone"]["tone_fit"] for r in runs if "tone" in r]
        fa = [r["first_audio_ms"] for r in runs if r["first_audio_ms"]]
        summary[sid] = {
            "n": len(runs),
            "companion_score_mean": round(statistics.mean(scores), 2) if scores else None,
            "companion_score_min": min(scores) if scores else None,
            "tone_fit_mean": round(statistics.mean(tones), 2) if tones else None,
            "chatbot_like_any": any(r["judge"]["chatbot_like"] for r in runs if "judge" in r),
            "banned_any": any(bool(r["banned_in_reply"]) for r in runs),
            "empty_reply_any": any(r["empty_reply"] for r in runs),
            "ran_context_intent_all": all(r["ran_context_intent"] for r in runs),
            "llm_calls_mean": round(statistics.mean([r["llm_calls"] for r in runs]), 2),
            "searches_mean": round(statistics.mean([r["searches"] for r in runs]), 2),
            "discarded_drafts_mean": round(
                statistics.mean([r["discarded_drafts"] for r in runs]), 2
            ),
            "cache_hits_total": sum(r["cache_hits"] for r in runs),
            "first_audio_ms_mean": round(statistics.mean(fa), 1) if fa else None,
            "total_ms_mean": round(statistics.mean([r["total_ms"] for r in runs]), 1),
            "cost_usd_mean": round(statistics.mean([r["cost_usd"] for r in runs]), 6),
            "exceptions_any": any(r["exceptions"] for r in runs),
        }
    return summary


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--repeats", type=int, default=REPEATS)
    ap.add_argument("--skip-tone-gate", action="store_true")
    args = ap.parse_args()

    pipeline = await build_pipeline(get_settings())
    scenarios = SCENARIOS
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        scenarios = [s for s in SCENARIOS if s.id in want]

    records: list[dict[str, Any]] = []
    for sc in scenarios:
        for run in range(args.repeats):
            rec = await _run(pipeline, sc, run)
            records.append(rec)
            print(
                f"  {sc.id}[{run}] calls={rec['llm_calls']} search={rec['searches']} "
                f"fa={rec['first_audio_ms']}ms exc={rec['exceptions']} "
                f"→ {rec['reply'][:66]!r}"
            )

    print("\njudging…")
    await _judge_all(pipeline, records)

    tone = None
    if not args.skip_tone_gate:
        print("dynamic-tone gate…")
        tone = await _tone_gate(pipeline)

    summary = _summarize(records)
    OUT_DIR.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — one-shot script
    path = OUT_DIR / f"{args.label}.json"
    path.write_text(
        json.dumps(
            {
                "label": args.label,
                "entrypoint": "VoiceSession.converse (the live path)",
                "summary": summary,
                "tone_gate": tone,
                "records": records,
            },
            indent=2,
        )
    )
    print(f"\nwrote {path}\n")
    for sid, s in summary.items():
        print(
            f"{sid:20} score={s['companion_score_mean']} tone={s['tone_fit_mean']} "
            f"chatbot={s['chatbot_like_any']} banned={s['banned_any']} "
            f"empty={s['empty_reply_any']} calls={s['llm_calls_mean']} "
            f"fa={s['first_audio_ms_mean']}ms"
        )
    if tone:
        print(f"\ntone gate: distinct={tone['all_distinct']} min_fit={tone['min_tone_fit']}")
        for v in tone["variants"]:
            print(
                f"  {v['emotion']:8} [{v['register']:8}] fit={v['tone_fit']} :: {v['reply'][:70]!r}"
            )


if __name__ == "__main__":
    asyncio.run(main())
