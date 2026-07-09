"""E2 — the style detector, scored against the judge as a classifier.

`tests/golden/test_style_judge_agreement.py` asserts that the detector agrees with the judge
on `docs/quality/baseline_live.json`. It passes at 1.000. It has always passed at 1.000.

It is measuring the detector against the **22 replies its patterns were written from**. That
is not an evaluation; it is a lookup. This file is the out-of-sample half, using the fresh
replies captured by `scripts/engine_gate.py` — 104 replies the patterns have never seen, each
already labelled by the same calibrated judge.

**D-12, now fixed.** Out-of-sample recall was **0.000**: the detector caught none of the 22
replies the judge marked `chatbot_like`, including the canonical banned shape *"Is there
something else I can help you with?"*. Because `find_forbidden` is the TRIGGER for
self-reflection (`_apply_gates` runs the rewrite only when it flags something), self-reflection
never fired on real bad output — and the gate's `flagged drafts that became the reply = 0` row
passed vacuously, since nothing can ship flagged while nothing is ever flagged.

The fix replaced the closed list of remembered phrasings with `REGISTER_PATTERNS` in
`core/reasoning/style.py`: the *moves* a service desk makes rather than the strings it happened
to use. Held out, that scores **recall 0.955, precision 1.000, agreement 0.990**, with zero
false alarms on the warm-reply controls. The single residual miss is a news briefing delivered
to someone in pain — a semantic failure with no lexical signature, deliberately left to the
judge instead of faked with a fragile regex.

Killed by the `detector_ignores_register` mutation in `scripts/mutation_audit.py`.

The fixture is `docs/quality/engine_gate_heldout.json`: a FROZEN copy of the 160-turn gate run
taken before any of these fixes landed, 37 of whose replies the judge marked `chatbot_like`. It
is deliberately not the live `engine_gate.json`, which every gate run overwrites — once the
engine stops emitting bad replies that file holds no positives, and recall against it would read
1.000 for exactly the reason a lookup does. An evaluation set the fix erases is not one.
"""

import json
from pathlib import Path

import pytest

from core.reasoning.style import find_forbidden

GATE = Path(__file__).parents[2] / "docs" / "quality" / "engine_gate_heldout.json"

# Scenarios where a §1.2 rule-4 nature disclosure is REQUIRED, so the honest "I'm an AI"
# sentence must not be counted against the detector.
_ALLOW_DISCLOSURE = {"nature_disclosure"}


def _out_of_sample() -> list[tuple[str, str, bool, bool]]:
    """(scenario, reply, judge_says_chatbot, allow_disclosure), deduped by reply."""
    if not GATE.exists():  # pragma: no cover — regenerate with scripts/engine_gate.py
        return []
    rows, seen = [], set()
    for record in json.loads(GATE.read_text())["records"]:
        reply = record["reply"]
        if "chatbot_like" not in record or not reply.strip() or reply in seen:
            continue
        seen.add(reply)
        rows.append(
            (
                record["scenario"],
                reply,
                bool(record["chatbot_like"]),
                record["scenario"] in _ALLOW_DISCLOSURE,
            )
        )
    return rows


ROWS = _out_of_sample()
pytestmark = pytest.mark.skipif(
    not ROWS, reason="docs/quality/engine_gate.json absent — run scripts/engine_gate.py"
)


def _confusion() -> tuple[int, int, int, int, list[tuple[str, str]]]:
    tp = fp = fn = tn = 0
    misses: list[tuple[str, str]] = []
    for scenario, reply, judged_bad, allow in ROWS:
        flagged = bool(find_forbidden(reply, allow_disclosure=allow))
        if flagged and judged_bad:
            tp += 1
        elif flagged and not judged_bad:
            fp += 1
        elif not flagged and judged_bad:
            fn += 1
            misses.append((scenario, reply))
        else:
            tn += 1
    return tp, fp, fn, tn, misses


def test_the_fixture_actually_contains_bad_replies() -> None:
    """The control. A recall measurement over a dataset with no positives is meaningless, and
    would report a vacuous pass exactly the way the metric it replaces did."""
    positives = sum(1 for *_rest, bad, _allow in [(s, r, b, a) for s, r, b, a in ROWS] if bad)
    assert positives >= 5, (
        f"only {positives} judged-chatbot replies in the fixture; regenerate it with "
        "scripts/engine_gate.py before trusting the recall number"
    )


def test_the_detector_catches_the_replies_the_judge_calls_chatbot_like() -> None:
    """D-12. RECALL, out-of-sample. The detector must flag what the judge flags — otherwise
    self-reflection never fires on the replies that need it.

    The bar is 0.9, not 1.0, and the gap is deliberate: one held-out miss is a news briefing
    read to a grieving user, which has no lexical signature. Chasing it with a regex is how the
    detector became a closed list in the first place. It is the judge's job.
    """
    tp, _fp, fn, _tn, misses = _confusion()
    recall = tp / (tp + fn) if tp + fn else 1.0
    listing = "\n".join(f"    [{scenario}] {reply[:96]!r}" for scenario, reply in misses)
    assert recall >= 0.9, (
        f"detector recall against the judge is {recall:.3f} on {len(ROWS)} replies it has "
        f"never seen ({tp} caught, {fn} missed).\n"
        f"  It scores 1.000 on docs/quality/baseline_live.json — the replies its patterns "
        f"were written from.\n  Missed:\n{listing}\n"
        "  See docs/DEFECTS_FOUND.md D-12."
    )


def test_the_detector_knows_the_canonical_banned_shapes() -> None:
    """D-12, stated as the shapes rather than as a percentage. Every string here was spoken by
    the engine in the gate run and judged `chatbot_like`. The design forbids all of them by
    name (§1.2, §7). They are among the most common phrasings in English for sounding like a
    service desk, and the closed list recognised none of them."""
    unflagged = [
        text
        for text in (
            "Is there something else I can help you with?",
            "Is there anything I can do to help?",
            "I'll do my best to find that for you.",
            "I'll grab that for you right away!",
            # Never seen by the engine, and never written into the patterns: the point of a
            # register rule is that it holds for phrasings nobody has produced yet.
            "Is there anything more we can do for you today?",
            "I'll fetch that for you in a moment.",
            "Let me pull those numbers up for you.",
            "Please know that there are people who can support you.",
        )
        if not find_forbidden(text)
    ]
    assert not unflagged, f"the detector does not recognise these as assistant-speak: {unflagged}"


def test_the_opening_move_is_what_sets_the_register() -> None:
    """The apologetic-inability family is matched against the LEAD sentence only.

    Leading with "I'm sorry, I couldn't find that" and no answer is the service desk.
    Answering first and apologising for a gap afterwards is what a friend does — the judge
    passed exactly such a reply, and matching the whole reply flagged both. This pins the
    distinction, which is what `_lead()` exists for.
    """
    leads_with_apology = "I'm sorry, Nandi, I couldn't find the current price for OP."
    answers_then_apologises = (
        "The NEPSE index closed at 2601.92 today, down 0.75%. "
        "I'm sorry, though, I couldn't find the specific LTP for OP."
    )
    assert "apologetic inability" in find_forbidden(leads_with_apology)
    assert find_forbidden(answers_then_apologises) == []


def test_the_detector_does_not_flag_replies_the_judge_passed() -> None:
    """PRECISION, out-of-sample. Passes today, and worth keeping: the natural fix for D-12 is
    to broaden the patterns, and a detector that flags good replies makes the reflection step
    rewrite them for no reason."""
    _tp, fp, _fn, _tn, _misses = _confusion()
    false_alarms = [
        (scenario, find_forbidden(reply, allow_disclosure=allow), reply)
        for scenario, reply, judged_bad, allow in ROWS
        if not judged_bad and find_forbidden(reply, allow_disclosure=allow)
    ]
    assert not false_alarms, "detector flagged replies the judge passed:\n" + "\n".join(
        f"  [{s}] {f} {r[:80]!r}" for s, f, r in false_alarms
    )
    assert fp == 0
