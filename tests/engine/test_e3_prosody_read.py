"""E3 — the seam between the reasoning step's emotional read and the delivery register.

Two engine steps meet here, and the bug lives in the join, not in either one:

    orchestrator `_resolve_note`   emits `emotional_read`, a free-text string
    `emotion_from_text`            maps that string to an EmotionRead
    `read_register`                maps that to a Register the responder is told to use

Each step passes its own unit tests. The seam does not. This is what E3 is for.

`_CONTEXT_INSTRUCTIONS` tells the model to emit `"emotional_read": "<the feeling, or
empty>"`. Models comply literally: on a neutral turn they write the word `"empty"`. The
sentinel list in `emotion_from_text` does not contain `"empty"`, so the string falls through
to the regex families — and the SAD family lists `empty`, meant for *"I feel empty"*.

Result: a greeting, and arithmetic, are delivered in a `down` register. Measured on the
spoken path over 3 runs, `"what's 15% of 240?"` selected `down` 3 times out of 3
(`docs/quality/caller_independence.json`).

The reverse half of the same defect: `"pain"` — the emotional read the design doc's own
worked example produces — matches no family at all and yields no emotion.

Both are D-5 in docs/DEFECTS_FOUND.md.
"""

import pytest

from core.reasoning.prosody import emotion_from_text, prosody_directive, read_register

# The strings a real `context_intent` call actually returns on a neutral turn. Taken from
# the probe logs, not invented: the prompt literally offers "empty" as the neutral value.
NEUTRAL_READS = ["", "empty", "neutral", "none", "calm", "n/a", "-", "no feeling"]

# Free-text reads a real `context_intent` call returns on turns that DO carry feeling.
# `pain` is the design doc's own example ("what's happening in Nepal … gives me a lot of
# pain"); `grief` and `dread` are the same register.
FELT_READS = {
    "sadness": "down",
    "grief": "down",
    "lonely": "down",
    "excitement": "excited",
    "proud": "excited",
    "stressed": "stressed",
    "anxious": "stressed",
    "frustrated": "stressed",
}


# ── the half that works ──────────────────────────────────────────────────────


@pytest.mark.parametrize("read,expected", sorted(FELT_READS.items()))
def test_a_felt_read_selects_the_register_that_feeling_implies(read: str, expected: str) -> None:
    emotion = emotion_from_text(read)
    assert emotion is not None, f"{read!r} produced no emotional signal at all"
    register, directive = prosody_directive(emotion)
    assert register == expected, f"{read!r} → {register!r}, expected {expected!r}"
    assert directive.strip(), "a register with no delivery directive changes nothing"


@pytest.mark.parametrize("read", ["", "neutral", "none", "calm", "n/a", "-", "no feeling"])
def test_a_recognised_neutral_read_forces_no_tone(read: str) -> None:
    """Never force a tone off a blank signal — design §3.6.5's "uncertain read → presence
    over prompting" applied to delivery."""
    assert emotion_from_text(read) is None
    assert read_register(emotion_from_text(read)) == "neutral"


# ── D-5: the seam ────────────────────────────────────────────────────────────


@pytest.mark.defect
def test_the_literal_word_empty_is_not_an_emotion() -> None:
    """D-5. `_CONTEXT_INSTRUCTIONS` offers `"<the feeling, or empty>"`, so `"empty"` IS the
    model's way of saying "no feeling". Parsing it as sadness inverts the whole mechanism.

    The fix is one sentinel, but the deeper lesson is that the prompt and the parser
    disagree about their shared vocabulary and nothing checked.
    """
    assert emotion_from_text("empty") is None, (
        f"the neutral sentinel 'empty' parsed as {emotion_from_text('empty')}. "
        "Every emotionally-neutral spoken turn is delivered in a 'down' register. "
        "See docs/DEFECTS_FOUND.md D-5."
    )


@pytest.mark.defect
def test_a_neutral_turn_is_never_delivered_in_a_down_register() -> None:
    """D-5, stated as the user-visible symptom: the companion answers "what's 15% of 240?"
    in a sad voice, 3 runs out of 3."""
    register = read_register(emotion_from_text("empty"))
    assert register == "neutral", (
        f"an emotional read of 'empty' selects the {register!r} register — every neutral "
        "spoken turn is delivered as if the user were sad"
    )


# Words a `context_intent` call really does return for a turn that carries pain. `pain`
# itself is the design doc's example: "what's happening in Nepal … gives me a lot of pain".
PAIN_READS = ["pain", "in pain", "hurting", "distress", "anguish"]


@pytest.mark.defect
def test_the_emotional_reads_the_design_doc_itself_produces_are_understood() -> None:
    """D-5, other half. Asserted as one set rather than per-word, so the failure names
    every unrecognised read at once instead of hiding the passing ones.

    `hurting` is understood only because the SAD family lists `hurt`. `pain`, the exact word
    the flagship indirect-emotional scenario produces, matches nothing — so the most
    emotionally loaded turn in the entire golden set is delivered in a neutral register.
    """
    unrecognised = [read for read in PAIN_READS if emotion_from_text(read) is None]
    assert not unrecognised, (
        f"these emotional reads produce NO signal, leaving the register neutral: "
        f"{unrecognised}. See docs/DEFECTS_FOUND.md D-5."
    )
    assert all(read_register(emotion_from_text(read)) == "down" for read in PAIN_READS)


# ── the sentinel list and the regex families must not overlap ────────────────


@pytest.mark.defect
def test_no_neutral_sentinel_is_also_an_emotion_word() -> None:
    """The property that would have caught D-5 the day it was written: a string the model
    uses to mean "no feeling" must never match a feeling. Checks every sentinel the prompt
    could plausibly elicit, in one assertion."""
    misparsed = {read: emotion_from_text(read) for read in NEUTRAL_READS}
    offenders = {read: emotion for read, emotion in misparsed.items() if emotion is not None}
    assert not offenders, f"neutral reads parsed as emotions: {offenders}"
