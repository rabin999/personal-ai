"""Unit tests for audio-awareness decision logic (brief U10/U11/U12)."""

from core.audio.awareness import (
    HealthMonitor,
    register_mirror_directive,
    surroundings_context,
)
from ports.sound import SoundRead


# ── U10: health-sound check-in (caring, no-nag) ───────────────────────────
def _cough(conf: float = 0.8) -> SoundRead:
    return SoundRead(health_sounds=["cough"], confidence=conf)


def test_confident_cough_triggers_a_caring_checkin() -> None:
    mon = HealthMonitor()
    result = mon.observe(_cough(), context_hint="monsoon weather, earlier cough")
    assert result.should_check_in
    assert "gently" in result.directive.lower() and "monsoon" in result.directive.lower()
    assert "cough" in result.observation  # stored as an episodic health note


def test_low_confidence_cough_does_not_fire() -> None:
    mon = HealthMonitor()
    assert not mon.observe(_cough(conf=0.2)).should_check_in


def test_no_nag_cooldown_between_checkins() -> None:
    """After checking in once, don't check in again every single cough (no nagging)."""
    mon = HealthMonitor()
    assert mon.observe(_cough()).should_check_in  # first fires
    # The next few coughs are noticed (counted) but NOT re-prompted.
    for _ in range(3):
        r = mon.observe(_cough())
        assert not r.should_check_in
        assert r.observation  # still tracked as an observation
    assert mon.counts["cough"] >= 4


def test_no_health_sound_never_checks_in() -> None:
    mon = HealthMonitor()
    assert not mon.observe(SoundRead(vocal_register="normal", confidence=0.9)).should_check_in
    assert not mon.observe(None).should_check_in


# ── U11: tone mirroring (setting-gated, live) ─────────────────────────────
def test_whisper_mirrored_when_setting_on() -> None:
    directive, reg = register_mirror_directive(SoundRead(vocal_register="whisper"), mimic_tone=True)
    assert reg == "whisper" and "whisper" in directive.lower()


def test_no_mirror_when_setting_off() -> None:
    """Setting off → always normal register regardless of the user's tone (live toggle)."""
    directive, reg = register_mirror_directive(
        SoundRead(vocal_register="whisper"), mimic_tone=False
    )
    assert directive == "" and reg is None


def test_normal_register_is_not_mirrored() -> None:
    directive, reg = register_mirror_directive(SoundRead(vocal_register="normal"), mimic_tone=True)
    assert directive == "" and reg is None


# ── U12: surroundings context + privacy gate ──────────────────────────────
def test_near_mode_ignores_ambient() -> None:
    read = SoundRead(ambient_voices=True, ambient_activity=True)
    assert surroundings_context(read, ambient_mode="near", transcribe_others=False) == ""


def test_surroundings_mode_surfaces_ambient_awareness() -> None:
    read = SoundRead(ambient_voices=True)
    ctx = surroundings_context(read, ambient_mode="surroundings", transcribe_others=False)
    assert "another person" in ctx.lower()
    # Privacy gate: ambient awareness must NOT imply transcribing others.
    assert "not transcribing" in ctx.lower()


def test_privacy_gate_off_by_default_note_absent_when_enabled() -> None:
    read = SoundRead(ambient_voices=True)
    ctx = surroundings_context(read, ambient_mode="surroundings", transcribe_others=True)
    assert "not transcribing" not in ctx.lower()  # user explicitly enabled it
