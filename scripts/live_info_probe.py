"""S1 evidence harness: does a current-affairs question actually trigger a search?

Runs each probe question through the LIVE voice path (`VoiceSession.converse`) and records
what the phrasing heuristic said, whether a search fired, the query used, and the answer.

The bug this exists to catch: routing hung off `_is_live_info_query`, a phrasing regex that
returns False for "who is the current prime minister of Nepal?". The turn then took the
non-agentic streaming path, could never reach a tool, and answered from training data.

Two of these are CONTROLS and must NOT search. Over-searching is its own failure.

Run:  PYTHONPATH=. uv run python -m scripts.live_info_probe before
      PYTHONPATH=. uv run python -m scripts.live_info_probe after
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from api.composition import build_pipeline
from config.settings import get_settings
from core.reasoning.response_gen import _is_live_info_query
from scripts.live_turn import drive_turn

USER = "u_demo_001"
OUT_DIR = Path("docs/quality")

# (question, must_search) — the last two are controls.
PROBES: list[tuple[str, bool]] = [
    ("who is the current prime minister of Nepal?", True),
    ("who is the president of the United States?", True),
    ("what's the LTP of SYPNL?", True),
    ("what's the price of SYPNL?", True),
    ("what's the weather in Kathmandu right now?", True),
    ("is Tim Cook still the CEO of Apple?", True),
    ("what happened in the news today?", True),
    ("what's 15% of 240?", False),  # control — arithmetic
    ("I'm feeling low today", False),  # control — emotional
]


async def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    pipeline = await build_pipeline(get_settings())
    rows: list[dict[str, Any]] = []
    for question, must_search in PROBES:
        try:
            cap = await drive_turn(pipeline, USER, question)
            row = {
                "question": question,
                "must_search": must_search,
                "heuristic": _is_live_info_query(question),
                "searches": len(cap.searches),
                "queries": cap.searches,
                "reply": cap.reply_text,
                "purposes": cap.purposes,
                "ok": (len(cap.searches) > 0) == must_search,
            }
        except Exception as exc:
            row = {
                "question": question,
                "must_search": must_search,
                "error": repr(exc),
                "ok": False,
            }
        rows.append(row)
        print(
            f"[heur={row.get('heuristic')!s:5}] searches={row.get('searches')} "
            f"{'OK ' if row['ok'] else 'BAD'} {question}",
            flush=True,
        )
        print(f"    query : {row.get('queries')}", flush=True)
        print(f"    reply : {str(row.get('reply', row.get('error')))[:130]}", flush=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — one-shot script
    (OUT_DIR / f"live_info_{label}.json").write_text(json.dumps(rows, indent=2))
    passed = sum(1 for r in rows if r["ok"])
    print(f"\n{passed}/{len(rows)} probes behave correctly  -> docs/quality/live_info_{label}.json")


if __name__ == "__main__":
    asyncio.run(main())
