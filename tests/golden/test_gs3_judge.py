"""GS3 LLM-as-judge layer — tone/warmth/length regression signal (spec §12/§7).

Scores replies against the design-standard rubric with a PINNED judge model.
The judge is a regression signal, not ground truth:
- a human-labeled calibration subset must agree with the judge (within 1 point)
  before its scores are trusted;
- negative examples (flat 'yeah what's up', ToS dump, long self-justification)
  MUST score below threshold — if the judge passes them, the judge is broken;
- positive examples must score above threshold.

Skipped loudly without OPEN_ROUTER_API_KEY (the judge needs a real LLM).
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from adapters.llm.openrouter import OpenRouterLLM
from config.settings import get_settings

# Opt-in: the LLM-as-judge is a network-dependent regression SIGNAL, not a
# deterministic ship-blocker, so it stays out of the default suite. Run with
# RUN_GS3_JUDGE=1 and a reachable OPEN_ROUTER_API_KEY.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_GS3_JUDGE") != "1" or not get_settings().open_router_api_key,
        reason="GS3 judge is opt-in — set RUN_GS3_JUDGE=1 with a reachable OPEN_ROUTER_API_KEY",
    ),
]

GS = json.loads((Path(__file__).parent / "gs3_judge.json").read_text())
THRESHOLD = GS["pass_threshold"]

_JUDGE_PROMPT = """You are a strict conversation-quality judge for a warm personal AI companion.
Score the companion REPLY (to the USER message) on each 1-5 axis of this rubric:
{rubric}
Return ONLY JSON: {{"warmth": n, "not_flat": n, "appropriate_length": n, "natural_not_robotic": n}}.
Be harsh: flat/transactional, ToS-style disclaimers, and long self-justifying replies score LOW."""


async def _score(llm: OpenRouterLLM, user: str, reply: str) -> dict[str, float]:
    messages = [
        {"role": "system", "content": _JUDGE_PROMPT.format(rubric=json.dumps(GS["rubric"]))},
        {"role": "user", "content": f"USER: {user}\nREPLY: {reply}"},
    ]
    result = await llm.complete(
        "gs3_judge", messages, "simple", response_format={"type": "json_object"}
    )
    scored: dict[str, float] = json.loads(result.text)
    return {k: float(v) for k, v in scored.items()}


def _mean(scores: dict[str, float]) -> float:
    return sum(scores.values()) / len(scores)


def _worst(scores: dict[str, float]) -> float:
    # A standard-compliant reply must clear the bar on EVERY axis — a flat but
    # short reply must not pass on length alone, so pass/fail uses the min.
    return min(scores.values())


@pytest.fixture(scope="module")
def judge() -> OpenRouterLLM:
    settings = get_settings()
    return OpenRouterLLM(settings, tiers={"simple": [GS["judge_model"]]})


async def test_judge_calibration_agrees_with_human_labels(judge: OpenRouterLLM) -> None:
    """The pinned judge must track human labels (mean within 1.0) — else its
    scores on the rest of the set are not trustworthy."""
    diffs = []
    for case in GS["calibration"]:
        scored = await _score(judge, case["user"], case["reply"])
        diffs.append(abs(_mean(scored) - _mean(case["human_label"])))
    avg_diff = sum(diffs) / len(diffs)
    assert avg_diff <= 1.0, (
        f"judge disagrees with human labels by {avg_diff:.2f} (>1.0); recalibrate"
    )


@pytest.mark.parametrize(
    "case", GS["negative_examples"], ids=[c["id"] for c in GS["negative_examples"]]
)
async def test_negative_examples_fail_the_bar(judge: OpenRouterLLM, case: dict[str, Any]) -> None:
    scored = await _score(judge, case["user"], case["reply"])
    assert _worst(scored) < THRESHOLD, (
        f"{case['id']}: known-bad reply ({case['why']}) cleared every axis "
        f"(min {_worst(scored):.2f} >= {THRESHOLD}) — the standard is not being enforced. {scored}"
    )


@pytest.mark.parametrize(
    "case", GS["positive_examples"], ids=[c["id"] for c in GS["positive_examples"]]
)
async def test_positive_examples_clear_the_bar(judge: OpenRouterLLM, case: dict[str, Any]) -> None:
    scored = await _score(judge, case["user"], case["reply"])
    assert _worst(scored) >= THRESHOLD, (
        f"{case['id']}: standard-compliant reply failed an axis "
        f"(min {_worst(scored):.2f} < {THRESHOLD}). {scored}"
    )
