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


# ── C3: the text-sentiment fallback (U8 had never executed in production) ──


def test_text_sentiment_fallback_maps_reads_to_registers() -> None:
    """`ser_service_url` is empty in every deployment, so `prompt.emotion` was always None
    and `read_register` always returned "neutral". The reasoning step's own `emotional_read`
    now drives prosody instead."""
    from core.reasoning.prosody import emotion_from_text

    assert read_register(emotion_from_text("frustration and impatience")) == "stressed"
    assert read_register(emotion_from_text("deep sadness and grief")) == "down"
    assert read_register(emotion_from_text("excitement and joy")) == "excited"
    assert read_register(emotion_from_text("quiet contentment")) == "upbeat"


def test_text_sentiment_never_forces_a_tone_off_a_blank_signal() -> None:
    from core.reasoning.prosody import emotion_from_text

    for blank in ("", "   ", "neutral", "none", None):
        assert emotion_from_text(blank) is None
    assert read_register(emotion_from_text("")) == "neutral"


def test_text_sentiment_read_is_lower_confidence_than_acoustic() -> None:
    """It is the FLOOR, not a replacement for acoustic SER."""
    from core.reasoning.prosody import emotion_from_text

    read = emotion_from_text("she sounds really sad")
    assert read is not None
    assert read["source"] == "text"
    assert 0.3 <= read["confidence"] < 1.0
