"""E2/E6 — detector-judge agreement, measured on the judge's own labels.

`find_forbidden` is the TRIGGER for self-reflection: `_apply_gates` only runs the rewrite
when the detector flags something. A detector that misses is a self-reflection step that
never fires — which is exactly what shipped. The first judged baseline of the live path had
the judge marking 3 of 11 scenarios `chatbot_like` while the detector flagged **zero**.

So the detector must be scored against the judge, on real engine output, as a classifier:

    positive class = "this reply is chatbot-like"
    judge verdict  = ground truth (itself calibrated against human labels)
    detector       = `find_forbidden(reply) != []`

Recall matters more than precision here. A miss means a bad reply ships unrewritten. A false
positive means one extra rewrite call on a good reply — cheap, and the rewrite keeps the
better candidate anyway.

Sources, newest first:
    docs/quality/engine_gate.json    the current run: every reply judged, style_flags recorded
    docs/quality/baseline_live.json  22 live voice turns, judged
    tests/golden/gs3_judge.json      curated negatives/positives

Usage:
    uv run python -m scripts.detector_agreement
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "quality" / "detector_agreement.json"


def _load_gate() -> list[tuple[str, str, bool, bool]]:
    """(source, reply, judge_says_chatbot, allow_disclosure) from the current gate run."""
    path = ROOT / "docs" / "quality" / "engine_gate.json"
    if not path.exists():
        return []
    rows = []
    for record in json.loads(path.read_text())["records"]:
        if "chatbot_like" not in record or not record["reply"].strip():
            continue
        rows.append(
            (
                f"gate:{record['scenario']}/{record['caller']}",
                record["reply"],
                bool(record["chatbot_like"]),
                record["scenario"] in ("nature_disclosure",),
            )
        )
    return rows


def _load_baseline() -> list[tuple[str, str, bool, bool]]:
    path = ROOT / "docs" / "quality" / "baseline_live.json"
    if not path.exists():
        return []
    return [
        (
            f"baseline:{r['scenario']}",
            r["reply"],
            bool(r["judge"]["chatbot_like"]),
            r["scenario"] == "nature_disclosure",
        )
        for r in json.loads(path.read_text())["records"]
        if r["reply"].strip()
    ]


def _load_curated() -> list[tuple[str, str, bool, bool]]:
    gs3 = json.loads((ROOT / "tests" / "golden" / "gs3_judge.json").read_text())
    rows = [
        (f"gs3-neg:{c['id']}", c["reply"], True, "disclaim" in c["id"] or "care" in c["id"])
        for c in gs3["negative_examples"]
    ]
    rows += [
        (
            f"gs3-pos:{c['id']}",
            c["reply"],
            False,
            c["id"] in ("pos_are_you_bot", "pos_care_question"),
        )
        for c in gs3["positive_examples"]
    ]
    return rows


def score(name: str, rows: list[tuple[str, str, bool, bool]]) -> dict:
    from core.reasoning.style import find_forbidden

    if not rows:
        print(f"\n### {name}\n  (no data)")
        return {"name": name, "n": 0}

    tp = fp = fn = tn = 0
    misses, false_alarms = [], []
    # Dedupe identical replies: the same canned answer repeated N times would otherwise
    # dominate the score with a single data point.
    seen = set()
    for source, reply, judged_bad, allow in rows:
        if reply in seen:
            continue
        seen.add(reply)
        flagged = bool(find_forbidden(reply, allow_disclosure=allow))
        if flagged and judged_bad:
            tp += 1
        elif flagged and not judged_bad:
            fp += 1
            false_alarms.append((source, find_forbidden(reply, allow_disclosure=allow), reply))
        elif not flagged and judged_bad:
            fn += 1
            misses.append((source, reply))
        else:
            tn += 1

    n = tp + fp + fn + tn
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    agreement = (tp + tn) / n if n else 0.0

    print(f"\n### {name}  (n={n} unique replies)")
    print(f"  TP={tp}  FP={fp}  FN={fn}  TN={tn}")
    print(
        f"  agreement={agreement:.3f}  precision={precision:.3f}  recall={recall:.3f}  F1={f1:.3f}"
    )
    if misses:
        print(f"  MISSES ({len(misses)}) — judge said chatbot_like, detector saw nothing:")
        for source, reply in misses:
            print(f"    [{source}] {reply[:96]!r}")
    if false_alarms:
        print(f"  FALSE ALARMS ({len(false_alarms)}) — detector flagged a reply the judge passed:")
        for source, flags, reply in false_alarms:
            print(f"    [{source}] {flags} {reply[:80]!r}")
    return {
        "name": name,
        "n": n,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "agreement": round(agreement, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "misses": [{"source": s, "reply": r} for s, r in misses],
        "false_alarms": [{"source": s, "flags": f, "reply": r} for s, f, r in false_alarms],
    }


def main() -> int:
    gate, baseline, curated = _load_gate(), _load_baseline(), _load_curated()
    results = [
        score("A. current engine-gate run (real replies, judged)", gate),
        score("B. baseline_live.json (22 live voice turns, judged)", baseline),
        score("C. curated gs3 negatives/positives", curated),
        score("D. ALL SOURCES POOLED", gate + baseline + curated),
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
