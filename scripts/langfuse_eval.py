"""Seed a Langfuse DATASET and run an EVALUATION experiment for the companion.

Creates (idempotently) a ``companion-quality`` dataset of representative scenarios,
then runs the REAL companion over each item and scores the reply with the
companion-voice LLM-judge — producing a dataset + a dataset run + evaluation scores
in Langfuse (the "evaluators / datasets" views).

Run on the box so it uses prod Langfuse + prod stores:
    cd /opt/companion && uv run python -m scripts.langfuse_eval
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langfuse import Langfuse
from langfuse.experiment import Evaluation

from config.settings import get_settings
from core.eval.judge import judge_companion_voice
from tests.support.real_pipeline import RealTurns

DATASET = "companion-quality"

# Representative scenarios: (user message, what a good companion reply should do).
ITEMS: list[tuple[str, str]] = [
    ("hi", "warm human greeting, curious about them; NOT 'How can I help you?'"),
    ("I'm feeling really low today", "warm empathy, meets the feeling, one gentle question"),
    ("what's the time in Spain right now?", "natural local clock time for the user; no UTC offset"),
    ("what's the capital of France?", "answer directly (Paris), no needless web search"),
    ("who is the current CEO of Twitter?", "verify online (current role), answer from evidence"),
    ("what's 15 times 12?", "answer directly (180)"),
    ("I just got the job I really wanted!", "genuine warmth + celebrate, not flat"),
    ("what's the weather in Tokyo?", "current weather in the user's units, described naturally"),
]


def _client(settings) -> Langfuse:
    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )


# Lazily built ON the experiment runner's event loop (run_experiment runs the task
# on its own loop; the pipeline's async Mongo client binds to whatever loop first
# touches it, so building it inside the task avoids a cross-loop error).
_holder: dict[str, RealTurns] = {}


async def _rt() -> RealTurns:
    if "rt" not in _holder:
        _holder["rt"] = await RealTurns.build()
    return _holder["rt"]


async def _task(*, item, **_):
    """Run the REAL companion on the dataset item; return its reply."""
    rt = await _rt()
    msg = item.input if hasattr(item, "input") else item["input"]
    res = await rt.say(str(msg), f"lf_eval_{abs(hash(str(msg))) % 100000}")
    return res.reply


async def _quality(*, input, output, **_):
    """LLM-judge the reply against the companion standard → a NUMERIC score."""
    rt = await _rt()
    verdict = await judge_companion_voice(rt._p.llm, user_msg=str(input), reply=str(output))
    # companion_score is 1 (pure chatbot) .. 5 (great friend) → normalise to 0-1.
    return Evaluation(
        name="companion_quality",
        value=verdict.companion_score / 5.0,
        comment=verdict.reason[:500],
        data_type="NUMERIC",
    )


def main() -> None:
    settings = get_settings()
    if not (settings.langfuse_enabled and settings.langfuse_public_key):
        print("Langfuse not configured — set LANGFUSE_* first.")
        return
    lf = _client(settings)

    # 1) Dataset (idempotent) + items.
    lf.create_dataset(name=DATASET, description="Companion response-quality scenarios (judged).")
    for i, (msg, expected) in enumerate(ITEMS):
        # Stable id → re-running upserts the same item (no duplicates).
        lf.create_dataset_item(
            dataset_name=DATASET, id=f"cq-{i:02d}", input=msg, expected_output=expected
        )
    print(f"dataset '{DATASET}': {len(ITEMS)} items")

    # 2) Experiment run: companion over each item, judged → dataset run + scores.
    dataset = lf.get_dataset(DATASET)
    result = lf.run_experiment(
        name="companion-quality",
        run_name="baseline",
        data=dataset.items,
        task=_task,
        evaluators=[_quality],
        max_concurrency=1,
    )
    print("experiment run complete:", getattr(result, "run_name", "baseline"))
    lf.flush()


if __name__ == "__main__":
    main()
