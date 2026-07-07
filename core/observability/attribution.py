"""Performance attribution by prompt_version (Item 7 / spec §7).

Joins user feedback (thumbs up/down, each keyed to session_id + turn) with the
prompt_version recorded on that turn's assembly trace span, and rolls up a
thumbs-up rate per prompt_version — so a prompt/persona change (a version bump)
can be judged on real response quality instead of vibes.

Pure functions over already-fetched docs so they're trivially unit-testable; the
endpoint just supplies the feedback + trace events.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def prompt_version_by_turn(events: list[dict[str, Any]]) -> dict[tuple[str, int], str]:
    """(session_id, turn) → prompt_version, read from assembly spans in the trace."""
    out: dict[tuple[str, int], str] = {}
    for e in events:
        data = e.get("data", {}) or {}
        pv = data.get("prompt_version")
        if e.get("stage") == "assembly" and pv:
            out[(e.get("session_id", ""), int(e.get("turn", 0)))] = str(pv)
    return out


def attribute_by_prompt_version(
    feedback: list[dict[str, Any]],
    version_by_turn: dict[tuple[str, int], str],
    *,
    judge_scores: dict[tuple[str, int], float] | None = None,
) -> list[dict[str, Any]]:
    """Aggregate thumbs-up-rate (and optional judge score) grouped by prompt_version.

    Feedback whose turn can't be matched to a prompt_version is bucketed under
    "unknown" rather than dropped, so nothing is silently lost."""
    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"up": 0, "down": 0, "judge_sum": 0.0, "judge_n": 0}
    )
    for fb in feedback:
        session = fb.get("session_id", "")
        turn_raw = fb.get("turn_id")
        try:
            turn = int(turn_raw) if turn_raw is not None else 0
        except (TypeError, ValueError):
            turn = 0
        version = version_by_turn.get((session, turn), "unknown")
        bucket = agg[version]
        if fb.get("rating") == "up":
            bucket["up"] += 1
        elif fb.get("rating") == "down":
            bucket["down"] += 1
        if judge_scores is not None and (session, turn) in judge_scores:
            bucket["judge_sum"] += judge_scores[(session, turn)]
            bucket["judge_n"] += 1

    rows: list[dict[str, Any]] = []
    for version, b in agg.items():
        n = b["up"] + b["down"]
        rows.append(
            {
                "prompt_version": version,
                "thumbs_up": b["up"],
                "thumbs_down": b["down"],
                "n": n,
                "up_rate": round(b["up"] / n, 3) if n else None,
                "avg_judge_score": (
                    round(b["judge_sum"] / b["judge_n"], 2) if b["judge_n"] else None
                ),
            }
        )
    # Best-performing first (by up_rate, then volume); "unknown" sinks last.
    rows.sort(key=lambda r: (r["prompt_version"] == "unknown", -(r["up_rate"] or -1), -r["n"]))
    return rows
