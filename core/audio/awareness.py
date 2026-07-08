"""Audio-awareness decision logic (brief U10/U11/U12).

Pure decisions over a ``SoundRead`` (from the swappable ``SoundClassifier`` stage),
so they're deterministic and unit-testable independent of the acoustic model:

- **HealthMonitor (U10):** decide WHEN to gently check in on cough/sneeze/sniffle —
  biased to confident/repeated detections, never nagging (once per cooldown), and
  produces a caring, context-aware directive (correlate with weather/time/an earlier
  cough) plus an EPISODIC health observation (a transient note, not a durable "is
  sick" fact).
- **register_mirror_directive (U11):** when the ``mimic_tone`` setting is on and the
  user goes off their baseline register (whisper/soft), steer the reply to mirror it
  — live, per turn. When off, always normal.
- **surroundings_context (U12):** in "surroundings" mode, turn ambient signals into
  awareness context; in "near" mode, ignore them. Never transcribe other people
  unless the explicit privacy setting is on.

These emit directives/observations consumed by prompt assembly + prosody; the mic
capture that feeds the classifier is the pipeline/hardware side.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ports.sound import SoundRead

# U10: don't nag. Require a confident read, and only check in once per cooldown of
# consecutive detections; a lone low-confidence blip never fires.
_HEALTH_MIN_CONFIDENCE = 0.5
_HEALTH_COOLDOWN_TURNS = 6


@dataclass
class HealthCheckin:
    should_check_in: bool
    sound: str = ""
    directive: str = ""  # guidance woven into the reply when checking in
    observation: str = ""  # episodic health note to store (transient, not durable)


@dataclass
class HealthMonitor:
    """Per-session cough/sneeze tracker with a no-nag cooldown (U10)."""

    _turns_since_checkin: int = _HEALTH_COOLDOWN_TURNS
    _seen: int = 0
    counts: dict[str, int] = field(default_factory=dict)

    def observe(self, read: SoundRead | None, *, context_hint: str = "") -> HealthCheckin:
        """Fold in one turn's read; return whether/how to check in this turn."""
        self._turns_since_checkin += 1
        if read is None or not read.health_sounds or read.confidence < _HEALTH_MIN_CONFIDENCE:
            return HealthCheckin(should_check_in=False)
        sound = read.health_sounds[0]
        self._seen += 1
        self.counts[sound] = self.counts.get(sound, 0) + 1
        observation = f"user was {sound}ing during the conversation"
        # Cooldown: check in on the first confident detection, then not again until the
        # cooldown elapses — noticing, not nagging every cough.
        if self._turns_since_checkin < _HEALTH_COOLDOWN_TURNS:
            return HealthCheckin(should_check_in=False, sound=sound, observation=observation)
        self._turns_since_checkin = 0
        correlate = (
            f" Connect it naturally to the context ({context_hint}) rather than a generic prompt."
            if context_hint
            else ""
        )
        directive = (
            f"You've noticed the user {sound}ing a few times. GENTLY and caringly check in on how "
            f"they're feeling — non-clinical, one short line, not alarmed and not repetitive."
            + correlate
        )
        return HealthCheckin(
            should_check_in=True, sound=sound, directive=directive, observation=observation
        )


def register_mirror_directive(
    read: SoundRead | None, *, mimic_tone: bool
) -> tuple[str, str | None]:
    """U11: (directive, mirrored_register) for this turn. When ``mimic_tone`` is on and
    the user is off-baseline (whisper/soft), steer the reply to mirror it; else normal.
    Read per turn so toggling the setting takes effect on the very next reply."""
    if not mimic_tone or read is None:
        return "", None
    if read.vocal_register == "whisper":
        return (
            "The user is WHISPERING. Mirror it: reply in a soft, hushed, quiet register "
            "(use [whisper]/[soft], short and gentle) to the extent the voice supports it.",
            "whisper",
        )
    if read.vocal_register == "soft":
        return (
            "The user is speaking softly/low. Mirror their register: keep the reply soft "
            "and quiet ([soft], gentle).",
            "soft",
        )
    return "", None


def surroundings_context(
    read: SoundRead | None, *, ambient_mode: str, transcribe_others: bool
) -> str:
    """U12: ambient awareness context for the prompt. Only in "surroundings" mode; in
    "near" mode ambient is ignored (foreground-only). NEVER implies transcribing other
    people — that's gated separately by ``transcribe_others`` (default off)."""
    if ambient_mode != "surroundings" or read is None:
        return ""
    bits: list[str] = []
    if read.ambient_voices:
        bits.append("another person may be present nearby")
    if read.ambient_activity:
        bits.append("there's some ambient activity/sound around the user")
    if not bits:
        return ""
    note = "; ".join(bits)
    privacy = (
        ""
        if transcribe_others
        else " You are NOT transcribing anyone else — treat this only as ambient awareness."
    )
    return (
        f"Ambient awareness (surroundings mode): {note}. Let it gently inform your response "
        f"where relevant, without prying.{privacy}"
    )
