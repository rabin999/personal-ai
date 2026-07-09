"""S5 — does the per-turn judge slow down the NEXT turn?

The evaluator fires `asyncio.create_task(judge_companion_voice(...))` after every turn. It is
off the reply path, but it used to share the live turn's OpenRouter client, so its request
competed for a connection with the following turn. It now has its own `OpenRouterLLM` (its
own `AsyncOpenAI` client, hence its own HTTP connection pool).

This measures the claim rather than asserting it: N sequential real voice turns with the
judge ON vs OFF, reporting median and p95 of the *following* turn's latency.

Single samples measure noise — `context_intent` alone varied 1872 ms → 6985 ms on identical
input — so N defaults to 6 and only median/p95 are reported.

Run:  PYTHONPATH=. uv run python -m scripts.judge_contention
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from typing import Any

from api.composition import build_pipeline
from config.settings import get_settings
from scripts.live_turn import drive_turn

USER = "u_demo_001"
# Short, tool-free, memory-light turns so the judge is the only variable.
TURNS = ["hi", "thanks", "how's things", "hey again", "morning", "yo"]


async def _series(pipeline: Any, n: int) -> list[float]:
    """Latency of each turn, in order. Turn i is preceded by turn i-1's judge task."""
    out: list[float] = []
    for i in range(n):
        cap = await drive_turn(pipeline, USER, TURNS[i % len(TURNS)])
        out.append(cap.total_ms)
    return out


def _report(name: str, xs: list[float]) -> dict[str, float]:
    ordered = sorted(xs)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    stats = {"median": statistics.median(xs), "p95": p95, "mean": statistics.mean(xs)}
    print(
        f"  {name:22} median={stats['median']:8.0f} ms   p95={stats['p95']:8.0f} ms   n={len(xs)}"
    )
    return stats


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6)
    args = ap.parse_args()

    pipeline = await build_pipeline(get_settings())
    evaluator = pipeline.evaluator
    assert evaluator is not None, "no evaluator wired"
    print(f"evaluator enabled={evaluator.enabled} sample_rate={evaluator.sample_rate}\n")

    # Judge OFF first so its warm-up cost doesn't land on the ON series.
    evaluator._enabled = False
    off = await _series(pipeline, args.n)
    evaluator._enabled = True
    on = await _series(pipeline, args.n)

    print("turn latency, judge OFF vs ON (the judge runs on the PRECEDING turn):")
    s_off = _report("judge OFF", off)
    s_on = _report("judge ON", on)
    delta = s_on["median"] - s_off["median"]
    print(f"\n  median delta = {delta:+.0f} ms  ({delta / s_off['median']:+.1%})")
    print(
        "  verdict: "
        + (
            "no measurable contention"
            if abs(delta) < 0.10 * s_off["median"]
            else "POSSIBLE CONTENTION — investigate"
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
