"""Latency + quality benchmark across the top fast LLMs (final deliverable).

For each candidate fast model, run a few representative companion replies through the
REAL LLM (same spoken-reply system prompt the app uses), measuring per-call latency and
judging companion-voice quality - so model choice is a data-backed decision, not a
guess. Writes docs/LATENCY_BENCHMARK.md.

Run:  cd /opt/companion && uv run python -m scripts.latency_benchmark
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.composition import build_pipeline
from config.settings import get_settings
from core.eval.judge import judge_companion_voice
from core.reasoning.response_gen import _SPOKEN_REPLY_INSTRUCTIONS

# Fast/reply-tier candidates (L5): fast, high-quality small/flagship models. Confirmed
# on OpenRouter's catalog at write time; skipped automatically if a model 404s.
CANDIDATES = [
    "google/gemini-2.5-flash",
    "google/gemini-2.5-flash-lite",
    "anthropic/claude-haiku-4.5",
    "openai/gpt-4.1-mini",
    "openai/gpt-4.1-nano",
]

# Representative turns: a casual greeting, an emotional vent, a factual/analytical ask.
PROMPTS = [
    "hey, how's it going?",
    "ugh, today was rough - work totally drained me.",
    "should I take a break this weekend or push through?",
]
RUNS = 3  # per (model, prompt); report the median latency


async def _bench_one(pipeline, model: str) -> dict | None:
    lats: list[float] = []
    scores: list[int] = []
    chatbot = 0
    sample = ""
    for prompt_text in PROMPTS:
        messages = [
            {"role": "system", "content": _SPOKEN_REPLY_INSTRUCTIONS},
            {"role": "user", "content": prompt_text},
        ]
        reply = ""
        for _ in range(RUNS):
            t0 = time.perf_counter()
            try:
                res = await pipeline.llm.complete(
                    "u_bench",
                    messages,
                    "simple",
                    model=model,
                    temperature=0.7,
                    max_tokens=300,
                    reasoning={"enabled": False},
                    purpose="benchmark",
                )
            except Exception as exc:
                print(f"  {model}: FAILED ({type(exc).__name__}) — skipping")
                return None
            lats.append((time.perf_counter() - t0) * 1000)
            reply = res.text
        try:
            v = await judge_companion_voice(pipeline.llm, prompt_text, reply)
            scores.append(v.companion_score)
            chatbot += 1 if v.chatbot_like else 0
        except Exception:
            pass
        sample = reply
    return {
        "model": model,
        "median_ms": round(statistics.median(lats)),
        "p_min_ms": round(min(lats)),
        "p_max_ms": round(max(lats)),
        "avg_score": round(statistics.mean(scores), 1) if scores else 0.0,
        "chatbot_flags": chatbot,
        "sample": sample[:120],
    }


async def main() -> None:
    pipeline = await build_pipeline(get_settings())
    try:
        rows = []
        for model in CANDIDATES:
            print(f"benchmarking {model} ...")
            r = await _bench_one(pipeline, model)
            if r:
                rows.append(r)
        rows.sort(key=lambda r: r["median_ms"])
        lines = [
            "# Fast-LLM Latency + Quality Benchmark",
            "",
            f"_Generated {time.strftime('%Y-%m-%d %H:%M')} - {RUNS} runs x {len(PROMPTS)} "
            "representative companion turns per model (reply tier, thinking off, temp 0.7, "
            "max_tokens 300). Latency is the per-call round-trip through OpenRouter; quality "
            "is the companion-voice LLM-judge (1-5, higher better)._",
            "",
            "| Model | Median | Min | Max | Quality (avg /5) | Chatbot flags |",
            "|---|---|---|---|---|---|",
        ]
        for r in rows:
            lines.append(
                f"| `{r['model']}` | **{r['median_ms']}ms** | {r['p_min_ms']}ms | "
                f"{r['p_max_ms']}ms | {r['avg_score']} | {r['chatbot_flags']} |"
            )
        lines += [
            "",
            "## Notes",
            "- Ranked by median latency (fastest first).",
            "- Quality is scored by the pinned companion-voice judge on each reply; a good "
            "model is fast AND >=4/5 with 0 chatbot flags.",
            "- The app's default fast/reply tier is set in `config/defaults/provider_config."
            "json` (`llm_router.tiers`). Pick the fastest model that holds quality.",
            "",
            "### Sample replies",
        ]
        for r in rows:
            lines.append(f"- `{r['model']}` -> {r['sample']!r}")
        root = os.path.dirname(os.path.dirname(__file__))
        out = os.path.join(root, "docs", "LATENCY_BENCHMARK.md")
        with open(out, "w") as f:  # noqa: ASYNC230 - one-off script, not a hot path
            f.write("\n".join(lines) + "\n")
        print(f"\nwrote {out}")
        print("\n".join(lines[4:]))
    finally:
        await pipeline.aclose()


if __name__ == "__main__":
    asyncio.run(main())
