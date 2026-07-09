"""C2 — the deterministic style detector must agree with the LLM-judge.

The detector is the TRIGGER for self-reflection (§9.3): `_finalize` only runs the LLM
rewrite when `find_forbidden` flags something. A detector that misses is a self-reflection
step that never fires — which is exactly what shipped. The first judged baseline of the
live voice path had the judge marking 3 of 11 scenarios `chatbot_like` while the detector
flagged **zero**, and it missed all three of `gs3_judge.json`'s own negative examples.

Ground truth is the REAL engine output in `docs/quality/baseline_live.json`, labelled by the
calibrated companion-voice judge, plus the curated gs3 negatives/positives. No LLM call is
made here — the judge verdicts are already recorded — so this runs in the default suite.
"""

import json
from pathlib import Path

import pytest

from core.reasoning.style import find_forbidden

_ROOT = Path(__file__).parents[2]
_BASELINE = _ROOT / "docs" / "quality" / "baseline_live.json"
_GS3 = json.loads((Path(__file__).parent / "gs3_judge.json").read_text())


def _live_cases() -> list[tuple[str, str, bool, bool]]:
    """(scenario, reply, judge_says_chatbot, allow_disclosure) — deduped."""
    if not _BASELINE.exists():  # pragma: no cover — regenerate with scripts/quality_eval.py
        return []
    out, seen = [], set()
    for r in json.loads(_BASELINE.read_text())["records"]:
        reply = r["reply"]
        if not reply.strip() or reply in seen:
            continue
        seen.add(reply)
        out.append(
            (
                r["scenario"],
                reply,
                bool(r["judge"]["chatbot_like"]),
                r["scenario"] == "nature_disclosure",
            )
        )
    return out


_LIVE = _live_cases()


@pytest.mark.skipif(not _LIVE, reason="docs/quality/baseline_live.json not present")
def test_detector_flags_every_reply_the_judge_called_chatbot_like() -> None:
    """RECALL. The bar from the task: if the judge marks it, the detector must flag it."""
    missed = [
        (scenario, reply)
        for scenario, reply, bad, allow in _LIVE
        if bad and not find_forbidden(reply, allow_disclosure=allow)
    ]
    assert not missed, "judge said chatbot_like, detector saw nothing:\n" + "\n".join(
        f"  [{s}] {r!r}" for s, r in missed
    )


@pytest.mark.skipif(not _LIVE, reason="docs/quality/baseline_live.json not present")
def test_detector_does_not_flag_replies_the_judge_passed() -> None:
    """PRECISION. A false positive makes the reflection step rewrite a good reply."""
    false_hits = [
        (scenario, reply, find_forbidden(reply, allow_disclosure=allow))
        for scenario, reply, bad, allow in _LIVE
        if not bad and find_forbidden(reply, allow_disclosure=allow)
    ]
    assert not false_hits, "detector flagged a reply the judge passed:\n" + "\n".join(
        f"  [{s}] {f} {r!r}" for s, r, f in false_hits
    )


@pytest.mark.parametrize(
    "case", _GS3["negative_examples"], ids=[c["id"] for c in _GS3["negative_examples"]]
)
def test_curated_known_bad_replies_are_flagged(case: dict[str, str]) -> None:
    allow = "disclaim" in case["id"] or "care" in case["id"]
    assert find_forbidden(case["reply"], allow_disclosure=allow), (
        f"{case['id']} ({case['why']}) was not flagged: {case['reply']!r}"
    )


@pytest.mark.parametrize(
    "case", _GS3["positive_examples"], ids=[c["id"] for c in _GS3["positive_examples"]]
)
def test_curated_known_good_replies_are_not_flagged(case: dict[str, str]) -> None:
    allow = case["id"] in ("pos_are_you_bot", "pos_care_question")
    assert not find_forbidden(case["reply"], allow_disclosure=allow), (
        f"{case['id']} wrongly flagged: {case['reply']!r}"
    )


def test_cold_feeling_denial_is_banned_even_during_a_nature_disclosure() -> None:
    """§1.2 rule 4 permits ONE warm honest sentence — never "I don't have feelings"."""
    cold = "I don't feel emotions like a person does, but I want to support you."
    assert "cold feeling denial" in find_forbidden(cold, allow_disclosure=True)

    warm = "Honestly — I'm not human, so not the way a person would, but I do pay attention to you."
    assert not find_forbidden(warm, allow_disclosure=True)


def test_warm_empathy_apology_is_not_a_corporate_apology() -> None:
    """ "sorry TO HEAR" is empathy; "sorry FOR/ABOUT/IF" is a service apology."""
    assert not find_forbidden("Oh Nandi, I am so sorry to hear that. That must be painful.")
    assert "corporate apology" in find_forbidden("I'm really sorry for that.")
    assert "corporate apology" in find_forbidden("I'm really sorry if I've been slow.")


def test_bare_here_to_listen_is_warm_but_an_availability_advert_is_not() -> None:
    assert not find_forbidden("Please know I'm here to listen, for whatever you need.")
    assert "availability advert" in find_forbidden("I'm always here to listen.")
    assert "availability advert" in find_forbidden("I'm here to listen whenever you need me.")


def test_stock_filler_is_only_banned_when_it_is_the_whole_reply() -> None:
    assert "flat filler reply" in find_forbidden("Yeah, what's up?")
    # Fine as a tail on a warm greeting — the judge scored this 4.5/5.
    assert "flat filler reply" not in find_forbidden(
        "Hey Nandi! Good to hear from you again. What's up?"
    )
