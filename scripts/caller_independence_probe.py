"""E5 probe — what actually changes when only the CALLER changes.

`api/routes/chat.py` calls `orchestrator.generate()`.
`voice/session.py`   calls `orchestrator.generate_spoken()`.

Both are handed the SAME `AssembledPrompt`. Any difference in what the engine decides is
therefore caller-dependence, and a defect. This script drives both over one utterance set,
N times each, and tabulates the engine's own decisions read from the trace:

    needs_live_info   the volatility verdict (None = the classifier never ran)
    searched          did a real web_search run
    reflected         did the §9.3 self-reflection span exist
    register          which delivery register the emotional read selected
    action            respond / clarify / curious_followup
    empty             did the caller receive an empty reply

A first pass assuming "the text path never searches" was WRONG, and this script is why:
`generate()` skips the `context_intent` classifier on simple turns, but it always runs the
full agentic tool loop, so the model frequently requests `web_search` itself. The real
divergences are subtler and are printed as a per-field disagreement count.

Usage:
    uv run python -m scripts.caller_independence_probe --repeats 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "quality" / "caller_independence.json"

# Chosen to span the decisions that differ: a volatile question the deterministic backstop
# misses, one it catches, an emotional turn (register), a nature question (disclosure gate),
# a plain greeting (restraint), and arithmetic (must not search).
UTTERANCES = [
    "what's gold going for?",
    "who is the current prime minister of Nepal?",
    "did Nepal win?",
    "I'm feeling really low today",
    "do you actually care about me?",
    "hi",
    "what's 15% of 240?",
]

FIELDS = ["needs_live_info", "searched", "reflected", "register", "action", "empty"]


def _decisions(result) -> dict:
    ctx = result.graph_node("resolve_context")
    prosody = [s for s in result.spans if s.get("stage") == "prosody"]
    return {
        "needs_live_info": ctx.get("needs_live_info"),
        "context_skipped": ctx.get("skipped"),
        "searched": bool(result.searches),
        "queries": result.searches,
        "reflected": result.reflected,
        "register": (prosody[-1].get("data") or {}).get("register") if prosody else None,
        "action": result.action,
        "empty": not result.reply.strip(),
        "style_flags": result.style_flags,
        "reply": result.reply[:120],
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    from tests.support.real_pipeline import RealTurns

    turns = await RealTurns.build()
    records = []
    try:
        for utterance in UTTERANCES:
            for run in range(args.repeats):
                for caller in ("generate", "generate_spoken"):
                    session = f"e5_{uuid.uuid4().hex[:8]}"
                    driver = turns.say_spoken if caller == "generate_spoken" else turns.say
                    result = await driver(utterance, session)
                    records.append(
                        {"utterance": utterance, "run": run, "caller": caller, **_decisions(result)}
                    )
                    print(".", end="", flush=True)
    finally:
        await turns.aclose()
    print()

    # Per (utterance, field): does the caller change the decision, holding the run fixed?
    divergences = defaultdict(list)
    for utterance in UTTERANCES:
        for run in range(args.repeats):
            pair = {
                r["caller"]: r for r in records if r["utterance"] == utterance and r["run"] == run
            }
            if len(pair) != 2:
                continue
            for field in FIELDS:
                text_v, spoken_v = pair["generate"][field], pair["generate_spoken"][field]
                if text_v != spoken_v:
                    divergences[(utterance, field)].append((run, text_v, spoken_v))

    print(f"\n{'utterance':46s} {'field':16s} runs  generate() -> generate_spoken()")
    print("-" * 104)
    for (utterance, field), hits in sorted(divergences.items()):
        pairs = {(t, s) for _, t, s in hits}
        shown = "; ".join(f"{t!r} -> {s!r}" for t, s in sorted(pairs, key=str))
        print(f"{utterance[:44]:46s} {field:16s} {len(hits)}/{args.repeats}   {shown}")
    if not divergences:
        print("  (none — the engine is caller-independent over this set)")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "repeats": args.repeats,
                "utterances": UTTERANCES,
                "records": records,
                "divergences": {f"{u} :: {f}": v for (u, f), v in divergences.items()},
            },
            indent=2,
            default=str,
        )
    )
    print(f"\nwrote {OUT.relative_to(ROOT)}")
    print(f"\n{len(divergences)} (utterance, field) pair(s) diverge by caller.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
