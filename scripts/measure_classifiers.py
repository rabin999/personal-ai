"""Classifier measurement on labeled data (engine test session, E2).

`_is_live_info_query` and its siblings are binary classifiers whose false-negatives
make the app factually wrong. They had never been measured — three hand-picked
examples in a unit test is not a measurement.

This script scores every volatility classifier in the engine against
`tests/labeled/volatility.jsonl` and reports precision / recall / F1 plus the
COMPLETE false-negative list (a false negative is a stale answer spoken as fact).

Three deterministic classifiers, plus the real one:

    A  is_volatile_question          the volatility.py question-shape backstop
    B  _is_live_info_query           the response_gen.py topic-keyword regex
    C  A or B                        the deterministic gate as `_requires_live_lookup`
                                     composes it when the LLM verdict is None/False
    D  needs_live_info (REAL LLM)    the orchestrator's context_intent node — the
                                     primary gate in production. Requires --real.

D is the number that matters: `_requires_live_lookup` returns True when D is True,
and falls back to C otherwise. So the engine's true recall is `D or C`.

Usage:
    uv run python -m scripts.measure_classifiers                 # A/B/C only, free
    uv run python -m scripts.measure_classifiers --real          # + D (paid, ~174 calls)
    uv run python -m scripts.measure_classifiers --real --limit 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "labeled" / "volatility.jsonl"
OUT = ROOT / "docs" / "quality" / "classifier_metrics.json"

# The gate the brief sets for the volatile class.
RECALL_GATE = 0.95


@dataclass
class Score:
    name: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    false_negatives: list[dict] = None  # type: ignore[assignment]
    false_positives: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.false_negatives = self.false_negatives or []
        self.false_positives = self.false_positives or []

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def over_trigger(self) -> float:
        """Fraction of STABLE questions that would trigger a needless search."""
        return self.fp / (self.fp + self.tn) if self.fp + self.tn else 0.0

    def add(self, row: dict, predicted: bool) -> None:
        gold = row["label"] == "volatile"
        if predicted and gold:
            self.tp += 1
        elif predicted and not gold:
            self.fp += 1
            self.false_positives.append(row)
        elif not predicted and gold:
            self.fn += 1
            self.false_negatives.append(row)
        else:
            self.tn += 1

    def report(self) -> None:
        print(f"\n### {self.name}")
        print(f"  TP={self.tp}  FP={self.fp}  FN={self.fn}  TN={self.tn}")
        gate = "PASS" if self.recall >= RECALL_GATE else "FAIL"
        print(
            f"  precision={self.precision:.3f}  recall={self.recall:.3f} [{gate} vs "
            f"{RECALL_GATE}]  F1={self.f1:.3f}  stable-over-trigger={self.over_trigger:.3f}"
        )
        print(f"  FALSE NEGATIVES ({len(self.false_negatives)}) — stale answer spoken as fact:")
        for r in self.false_negatives:
            print(f"    [{r['class']:24s}] {r['q']}")
        if self.false_positives:
            print(f"  FALSE POSITIVES ({len(self.false_positives)}) — needless web search:")
            for r in self.false_positives:
                print(f"    [{r['class']:24s}] {r['q']}")

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "over_trigger": round(self.over_trigger, 4),
            "recall_gate": RECALL_GATE,
            "recall_gate_pass": self.recall >= RECALL_GATE,
            "false_negatives": [r["q"] for r in self.false_negatives],
            "false_positives": [r["q"] for r in self.false_positives],
        }


def load() -> list[dict]:
    return [json.loads(line) for line in DATA.read_text().splitlines() if line.strip()]


def score_sync(name: str, fn: Callable[[str], bool], rows: list[dict]) -> Score:
    s = Score(name)
    for row in rows:
        s.add(row, bool(fn(row["q"])))
    return s


async def score_llm(
    rows: list[dict], concurrency: int = 6
) -> tuple[Score, list[dict], dict[str, bool | None]]:
    """Drive the REAL context_intent node — the classifier production routes on."""
    from adapters.orchestrator.langgraph_orchestrator import LangGraphOrchestrator
    from api.composition import build_pipeline
    from config.settings import get_settings
    from core.reasoning.prompt_assembly import AssembledPrompt

    pipe = await build_pipeline(get_settings())
    orch = pipe.orchestrator
    assert isinstance(orch, LangGraphOrchestrator), f"expected LangGraph engine, got {type(orch)}"

    sem = asyncio.Semaphore(concurrency)
    preds: dict[str, tuple[bool | None, str]] = {}

    async def one(row: dict) -> None:
        q = row["q"]
        prompt = AssembledPrompt(
            user_id="u_demo_001",
            session_id=f"vol_probe_{abs(hash(q)) % 10**8}",
            utterance=q,
            system_prompt="You are Companion.",
            messages=[
                {"role": "system", "content": "You are Companion."},
                {"role": "user", "content": q},
            ],
            complexity_hint="moderate",
        )
        async with sem:
            res = await orch._resolve_note(prompt)  # the real context_intent node
        preds[q] = (res.needs_live_info, res.live_query)

    t0 = time.time()
    await asyncio.gather(*(one(r) for r in rows))
    print(f"  ({len(rows)} real context_intent calls in {time.time() - t0:.0f}s)")
    await pipe.aclose()

    s = Score("D. needs_live_info — the REAL LLM classifier (context_intent node)")
    unusable = []
    for row in rows:
        verdict, _q = preds[row["q"]]
        if verdict is None:
            unusable.append(row)
        s.add(row, verdict is True)
    return s, unusable, {q: v for q, (v, _) in preds.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--real", action="store_true", help="also measure the real LLM classifier (paid)"
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--repeats", type=int, default=1, help="repeat the LLM classifier N times (drift)"
    )
    args = ap.parse_args()

    rows = load()
    if args.limit:
        rows = rows[: args.limit]
    print(
        f"labeled set: {len(rows)} questions "
        f"({sum(r['label'] == 'volatile' for r in rows)} volatile / "
        f"{sum(r['label'] == 'stable' for r in rows)} stable)"
    )

    from core.reasoning.response_gen import _is_live_info_query
    from core.reasoning.volatility import is_volatile_question

    scores = [
        score_sync("A. is_volatile_question (volatility.py backstop)", is_volatile_question, rows),
        score_sync(
            "B. _is_live_info_query (response_gen.py topic regex)", _is_live_info_query, rows
        ),
        score_sync(
            "C. A or B — the deterministic gate inside _requires_live_lookup",
            lambda q: is_volatile_question(q) or _is_live_info_query(q),
            rows,
        ),
    ]

    unusable: list[dict] = []
    drift: dict = {}
    if args.real:
        # The LLM classifier is stochastic: one run measures noise, not the classifier.
        # Repeat it and report per-question instability alongside the median recall.
        runs = []
        for i in range(args.repeats):
            s, u, p = asyncio.run(score_llm(rows))
            runs.append((s, u, p))
            print(f"  run {i + 1}/{args.repeats}: recall={s.recall:.3f} unusable={len(u)}")
        llm_score, unusable, llm_preds = runs[-1]
        recalls = sorted(s.recall for s, _, _ in runs)
        flaky = {
            r["q"]: [runs[i][2][r["q"]] for i in range(args.repeats)]
            for r in rows
            if len({runs[i][2][r["q"]] for i in range(args.repeats)}) > 1
        }
        drift = {
            "repeats": args.repeats,
            "recall_runs": [round(x, 4) for x in recalls],
            "recall_median": round(recalls[len(recalls) // 2], 4),
            "recall_min": round(recalls[0], 4),
            "recall_max": round(recalls[-1], 4),
            "unstable_questions": flaky,
        }
        if args.repeats > 1:
            print(
                f"\n  DRIFT over {args.repeats} runs: recall median="
                f"{drift['recall_median']:.3f} min={drift['recall_min']:.3f} "
                f"max={drift['recall_max']:.3f}"
            )
            print(f"  {len(flaky)} question(s) flipped verdict between runs:")
            for q, verdicts in flaky.items():
                print(f"    {verdicts} :: {q}")
        scores.append(llm_score)
        # E = exactly what `_requires_live_lookup` computes: the LLM verdict when it is
        # True, else the deterministic backstop. This is the number that decides whether
        # the user hears a stale answer.
        eff = Score("E. EFFECTIVE gate: needs_live_info OR (A or B)  ← what production routes on")
        for row in rows:
            q = row["q"]
            eff.add(
                row,
                llm_preds[q] is True or is_volatile_question(q) or _is_live_info_query(q),
            )
        scores.append(eff)

    for s in scores:
        s.report()

    if unusable:
        print(
            f"\n  context_intent returned an UNUSABLE verdict (None) for "
            f"{len(unusable)}/{len(rows)} questions ({len(unusable) / len(rows):.1%}):"
        )
        for r in unusable:
            print(f"    [{r['class']:24s}] {r['q']}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "dataset": str(DATA.relative_to(ROOT)),
                "n": len(rows),
                "recall_gate": RECALL_GATE,
                "unusable_llm_verdicts": [r["q"] for r in unusable],
                "llm_drift": drift,
                "classifiers": [s.as_dict() for s in scores],
            },
            indent=2,
        )
    )
    print(f"\nwrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
