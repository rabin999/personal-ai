"""Unit tests for dynamic prosody selection (brief U8)."""

from core.reasoning.prosody import (
    prosody_directive,
    read_register,
    strip_inappropriate_tags,
)


def test_sad_read_selects_gentle_encouraging_register() -> None:
    reg, directive = prosody_directive({"valence": -0.6, "arousal": -0.2, "confidence": 0.8})
    assert reg == "down"
    assert "gentle" in directive.lower() and "encouraging" in directive.lower()
    assert "never use [laugh]" in directive.lower() or "not" in directive.lower()


def test_stressed_read_selects_calm_register() -> None:
    reg, directive = prosody_directive({"valence": -0.4, "arousal": 0.6, "confidence": 0.8})
    assert reg == "stressed"
    assert "calm" in directive.lower() and "steady" in directive.lower()


def test_excited_read_selects_upbeat_register() -> None:
    reg, _ = prosody_directive({"valence": 0.7, "arousal": 0.7, "confidence": 0.9})
    assert reg == "excited"


def test_low_confidence_defaults_to_neutral() -> None:
    assert read_register({"valence": -0.8, "arousal": 0.0, "confidence": 0.1}) == "neutral"


def test_no_emotion_is_neutral() -> None:
    assert read_register(None) == "neutral"


def test_label_shortcut_sad() -> None:
    assert (
        read_register({"valence": 0.0, "arousal": 0.0, "label": "tired", "confidence": 0.9})
        == "down"
    )


def test_strip_levity_tags_on_sad_turn() -> None:
    """The exact bug: no laughing on a sad turn, even if the model added a laugh tag."""
    out = strip_inappropriate_tags("[laugh] that's rough, I'm here [gentle]", "down")
    assert "[laugh]" not in out
    assert "[gentle]" in out  # appropriate tags stay


def test_levity_tags_kept_on_excited_turn() -> None:
    text = "[laugh] that's amazing!"
    assert strip_inappropriate_tags(text, "excited") == text
