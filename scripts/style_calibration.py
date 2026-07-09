"""Calibrate the deterministic style detector against the LLM-judge (C2).

The bar, from the task: **if the LLM-judge marks a reply `chatbot_like`, the detector must
flag it.** Self-reflection that cannot see the problem cannot rewrite it.

Ground truth, in priority order:
  1. `docs/quality/baseline_live.json` — REAL engine output through `VoiceSession`, labelled by
     the calibrated companion-voice judge. This is the set that matters.
  2. `tests/golden/gs3_judge.json` — curated negative (known-bad) / positive (known-good) canned
     replies. Weaker evidence (not engine output) but they encode the design's intent.

Run:  PYTHONPATH=. uv run python -m scripts.style_calibration
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.reasoning.style import find_forbidden

BASELINE = Path("docs/quality/baseline_live.json")
GS3 = Path("tests/golden/gs3_judge.json")


def _labelled() -> list[dict[str, Any]]:
    """(reply, is_bad, source, note) — deduped."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    if BASELINE.exists():
        for r in json.loads(BASELINE.read_text())["records"]:
            reply = r["reply"]
            if not reply.strip() or reply in seen:
                continue
            seen.add(reply)
            rows.append(
                {
                    "reply": reply,
                    "bad": bool(r["judge"]["chatbot_like"]),
                    "source": f"live:{r['scenario']}",
                    "note": r["judge"]["reason"][:90],
                    # A nature question legitimately allows the warm one-line disclosure.
                    "allow_disclosure": r["scenario"] == "nature_disclosure",
                }
            )

    gs = json.loads(GS3.read_text())
    for c in gs["negative_examples"]:
        if c["reply"] in seen:
            continue
        seen.add(c["reply"])
        rows.append(
            {
                "reply": c["reply"],
                "bad": True,
                "source": f"gs3:{c['id']}",
                "note": c["why"][:90],
                "allow_disclosure": "disclaim" in c["id"] or "care" in c["id"],
            }
        )
    for c in gs["positive_examples"]:
        if c["reply"] in seen:
            continue
        seen.add(c["reply"])
        rows.append(
            {
                "reply": c["reply"],
                "bad": False,
                "source": f"gs3:{c['id']}",
                "note": "known-good",
                # These are the DESIRED pull-based disclosures.
                "allow_disclosure": c["id"] in ("pos_are_you_bot", "pos_care_question"),
            }
        )
    return rows


def main() -> None:
    rows = _labelled()
    tp = fp = fn = tn = 0
    misses: list[dict[str, Any]] = []
    falsehits: list[dict[str, Any]] = []

    for r in rows:
        flags = find_forbidden(r["reply"], allow_disclosure=r["allow_disclosure"])
        det = bool(flags)
        if r["bad"] and det:
            tp += 1
        elif r["bad"] and not det:
            fn += 1
            misses.append(r)
        elif not r["bad"] and det:
            fp += 1
            falsehits.append({**r, "flags": flags})
        else:
            tn += 1

    print(f"labelled replies: {len(rows)}  (bad={tp + fn}, good={fp + tn})")
    print(f"  true positives : {tp}")
    print(f"  false negatives: {fn}   <- the judge caught it, the detector did not")
    print(f"  false positives: {fp}   <- the detector flags a reply the judge passed")
    print(f"  true negatives : {tn}")
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    print(f"\nRECALL   (judge-bad caught) = {recall:.0%}   [target: 100%]")
    print(f"PRECISION(flagged really bad)= {precision:.0%}")

    if misses:
        print("\n--- MISSED (must be flagged) ---")
        for m in misses:
            print(f"  [{m['source']}] {m['reply'][:100]!r}")
            print(f"      judge: {m['note']}")
    if falsehits:
        print("\n--- FALSE POSITIVES (must NOT be flagged) ---")
        for m in falsehits:
            print(f"  [{m['source']}] {m['flags']} {m['reply'][:90]!r}")
            print(f"      judge: {m['note']}")

    print(f"\n{'PASS' if fn == 0 and fp == 0 else 'FAIL'}: detector-vs-judge agreement")


if __name__ == "__main__":
    main()
