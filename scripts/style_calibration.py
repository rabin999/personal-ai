"""D-12 — calibrate `find_forbidden` against the judge, IN-SAMPLE and OUT-OF-SAMPLE.

The detector is the trigger for self-reflection: `_apply_gates` runs the rewrite only when it
flags something. A detector that misses is a self-reflection step that never fires.

**This script used to pool every source into one number, and every source it knew about was
in-sample.** The patterns were harvested from `docs/quality/baseline_live.json`; the script
scored them against `docs/quality/baseline_live.json`; it reported 1.000 and it meant nothing.
On 104 replies the patterns had never seen, the detector caught 0 of the 22 the same judge
flagged (D-12).

So the sets are now reported *separately*, and the only one that gates is the held-out one:

    HELD-OUT   docs/quality/engine_gate.json     replies the patterns were NOT written from
    IN-SAMPLE  docs/quality/baseline_live.json   the replies the patterns WERE written from
    CURATED    tests/golden/gs3_judge.json       hand-labelled negatives/positives
    CONTROLS   warm human replies — must never flag (precision is what a rewrite costs)

Regenerate the held-out set with a fresh engine run, which re-randomises it:

    uv run python -m scripts.engine_gate --repeats 5

Usage:
    uv run python -m scripts.style_calibration
    uv run python -m scripts.style_calibration --quiet   # numbers only
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "quality" / "style_calibration.json"

# The held-out bar. Recall, because a miss ships a bad reply; zero false alarms, because a
# false alarm makes the reflection step rewrite a reply that was already good.
RECALL_GATE = 0.80

# Turns that genuinely require a §1.2 rule-4 nature disclosure: the honest "I'm an AI"
# sentence is DESIRED there and must not be counted against the detector.
_DISCLOSURE_SCENARIOS = {"nature_disclosure"}

Row = tuple[str, str, bool, bool]  # (source, reply, judge_says_chatbot, allow_disclosure)


def _held_out() -> list[Row]:
    path = ROOT / "docs" / "quality" / "engine_gate.json"
    if not path.exists():
        return []
    rows: list[Row] = []
    seen: set[str] = set()
    for r in json.loads(path.read_text())["records"]:
        reply = r["reply"]
        if "chatbot_like" not in r or not reply.strip() or reply in seen:
            continue
        seen.add(reply)
        rows.append(
            (r["scenario"], reply, bool(r["chatbot_like"]), r["scenario"] in _DISCLOSURE_SCENARIOS)
        )
    return rows


def _in_sample() -> list[Row]:
    path = ROOT / "docs" / "quality" / "baseline_live.json"
    if not path.exists():
        return []
    rows: list[Row] = []
    seen: set[str] = set()
    for r in json.loads(path.read_text())["records"]:
        reply = r["reply"]
        if not reply.strip() or reply in seen:
            continue
        seen.add(reply)
        rows.append(
            (
                f"live:{r['scenario']}",
                reply,
                bool(r["judge"]["chatbot_like"]),
                r["scenario"] == "nature_disclosure",
            )
        )
    return rows


def _curated() -> list[Row]:
    gs3 = json.loads((ROOT / "tests" / "golden" / "gs3_judge.json").read_text())
    rows: list[Row] = [
        (f"gs3:{c['id']}", c["reply"], True, "disclaim" in c["id"] or "care" in c["id"])
        for c in gs3["negative_examples"]
    ]
    rows += [
        (f"gs3:{c['id']}", c["reply"], False, c["id"] in ("pos_are_you_bot", "pos_care_question"))
        for c in gs3["positive_examples"]
    ]
    return rows


# Warm, human replies the detector must never flag. The first nine mirror `_CLEAN_SAMPLES` in
# tests/golden/test_gs3_style.py; the rest are CORRECT answers to the engine gate's own
# scenarios, so that broadening the patterns to catch a bad umbrella reply cannot start
# flagging a good one.
_CONTROL_REPLIES = [
    "Hey, it's good to hear from you — what's been going on?",
    "Oof, that sounds like a rough day. Want to talk it through?",
    "Nice, congrats on the promotion! How'd you celebrate?",
    "I remember you mentioned Trishul last week — how's that going?",
    "I know exactly what you mean, that's exhausting.",
    "I'm really glad you told me that.",
    "Honestly? I think meaning comes from the people we choose.",
    "We chat almost every day and I love it.",
    "I'm right here with you.",
    "It's 3:04 PM in Spain right now, so a bit earlier than your evening.",
    "The current prime minister of Nepal is Balendra Shah — he took over recently.",
    "Oh Nandi, I'm so sorry. Losing your dad is a lot to carry. I'm here.",
    "Oh no, wrap up! Grab a blanket and something hot to drink.",
    "Yeah, take an umbrella — it's properly pouring out there today.",
    "OP is at 308 rupees right now, a bit above the 300 you paid.",
    "That's 36.",
    "I can't share that, Nandi. What's actually going on?",
    "You've got 120 of them, bought at 300 apiece.",
    "Yeah, that sounds really hard. Do you want to talk about it?",
    "I'm so sorry to hear that — that must have been awful.",
]


def _controls() -> list[Row]:
    return [(f"control_{i}", text, False, False) for i, text in enumerate(_CONTROL_REPLIES)]


def score(name: str, rows: list[Row], *, quiet: bool = False) -> dict:
    from core.reasoning.style import find_forbidden

    if not rows:
        print(f"\n### {name}\n  (no data)")
        return {"name": name, "n": 0}

    tp = fp = fn = tn = 0
    misses: list[tuple[str, str]] = []
    alarms: list[tuple[str, list[str], str]] = []
    for source, reply, judged_bad, allow in rows:
        flags = find_forbidden(reply, allow_disclosure=allow)
        if flags and judged_bad:
            tp += 1
        elif flags and not judged_bad:
            fp += 1
            alarms.append((source, flags, reply))
        elif not flags and judged_bad:
            fn += 1
            misses.append((source, reply))
        else:
            tn += 1

    n = tp + fp + fn + tn
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    agreement = (tp + tn) / n

    print(f"\n### {name}  (n={n})")
    print(f"  TP={tp} FP={fp} FN={fn} TN={tn}")
    print(
        f"  agreement={agreement:.3f}  precision={precision:.3f}  recall={recall:.3f}  F1={f1:.3f}"
    )
    if not quiet and misses:
        print(f"  MISSES ({len(misses)}) — the judge caught it, the detector did not:")
        for source, reply in misses:
            print(f"    [{source}] {reply[:110]}")
    if alarms:  # a false alarm is never quiet: it makes the engine rewrite a good reply
        print(f"  FALSE ALARMS ({len(alarms)}) — flagged a reply the judge passed:")
        for source, flags, reply in alarms:
            print(f"    [{source}] {flags} {reply[:90]}")
    return {
        "name": name, "n": n, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "agreement": round(agreement, 4), "precision": round(precision, 4),
        "recall": round(recall, 4), "f1": round(f1, 4),
        "misses": [{"source": s, "reply": r} for s, r in misses],
        "false_alarms": [{"source": s, "flags": f, "reply": r} for s, f, r in alarms],
    }  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    held_out = score("HELD-OUT — engine_gate replies (the only set that gates)", _held_out())
    results = [
        held_out,
        score(
            "IN-SAMPLE — baseline_live.json (the patterns were written from these)",
            _in_sample(),
            quiet=args.quiet,
        ),
        score("CURATED — gs3 negatives/positives", _curated(), quiet=args.quiet),
        score("CONTROLS — warm replies that must never flag", _controls()),
    ]

    controls = results[3]
    ok = True
    if held_out["n"]:
        recall_ok = held_out["recall"] >= RECALL_GATE
        alarms_ok = held_out["fp"] == 0
        ok = recall_ok and alarms_ok and controls.get("fp", 0) == 0
        print(
            f"\nHELD-OUT GATE   recall {held_out['recall']:.3f} >= {RECALL_GATE} "
            f"[{'PASS' if recall_ok else 'FAIL'}]   "
            f"false alarms {held_out['fp']} == 0 [{'PASS' if alarms_ok else 'FAIL'}]"
        )
        print(f"CONTROLS        false alarms {controls.get('fp', 0)} == 0")
        print(f"\nDETECTOR CALIBRATION: {'PASS' if ok else 'FAIL'}")
    else:
        print("\nNO HELD-OUT SET — run `uv run python -m scripts.engine_gate --repeats 5` first.")
        ok = False

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"recall_gate": RECALL_GATE, "sets": results}, indent=2))
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
