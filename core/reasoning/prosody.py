"""Dynamic prosody selection (brief U8; design §10.2, §3.6.5).

The companion's voice must not be flat, and must not be MISMATCHED (the reported
bug: laughing on a sad turn). This maps the per-turn emotional read (SER
valence/arousal + text sentiment, §22) to (1) an explicit register directive
injected into generation so the model weaves the RIGHT delivery tags, and (2) a
deterministic backstop that strips clearly-inappropriate tags (a laugh on a
down/stressed turn) before TTS — the model's guidance plus a safety net.

Emotion is a probabilistic signal, never a diagnosis (§22 rule 4): a low-confidence
read defaults to a natural register rather than forcing a tone.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

Register = Literal["down", "stressed", "excited", "upbeat", "neutral"]

# Below this SER confidence we don't trust the read enough to steer tone (§22).
_MIN_CONFIDENCE = 0.3

# Tags that read as laughter/levity — never appropriate on a down or stressed turn.
_LEVITY_TAGS = ("[laugh]", "[chuckle]", "[giggle]", "[grin]", "[laughs]", "[laughter]")


def read_register(emotion: Mapping[str, Any] | None) -> Register:
    """Classify the emotional read into a delivery register. Neutral when there's no
    confident signal (never force a tone off a weak read)."""
    if not emotion:
        return "neutral"
    valence = _f(emotion.get("valence"))
    arousal = _f(emotion.get("arousal"))
    confidence = _f(emotion.get("confidence"), default=1.0)
    label = str(emotion.get("label", "")).lower()
    if confidence < _MIN_CONFIDENCE and not label:
        return "neutral"
    # Label shortcuts for clearly-signed emotions (SER's top categorical read).
    if label in ("sad", "tired", "down", "depressed", "disappointed"):
        return "down"
    if label in ("angry", "anxious", "stressed", "fear", "frustrated", "nervous"):
        return "stressed"
    if label in ("happy", "excited", "joy"):
        return "excited" if arousal >= 0.3 else "upbeat"
    # Otherwise use the dimensional read.
    if valence <= -0.2 and arousal >= 0.25:
        return "stressed"
    if valence <= -0.2:
        return "down"
    if valence >= 0.35 and arousal >= 0.35:
        return "excited"
    if valence >= 0.25:
        return "upbeat"
    return "neutral"


_DIRECTIVES: dict[Register, str] = {
    "down": (
        "The user sounds low/sad. Deliver in a WARM, GENTLE, quietly ENCOURAGING "
        "register that helps lift them — soft and unhurried. Use tags like [gentle], "
        "[warm], [soft], <slow>, <pause>. Do NOT sound cheery or upbeat, and NEVER "
        "use [laugh]/[chuckle] or any levity — that would be tone-deaf right now."
    ),
    "stressed": (
        "The user sounds stressed/anxious. Deliver CALM and STEADY — grounding, "
        "reassuring, slower. Use [gentle], [warm], <slow>, <pause>. Do NOT be bubbly "
        "or fast, and do NOT use [laugh]/[chuckle]."
    ),
    "excited": (
        "The user sounds excited. Match their energy — UPBEAT, warm, a little playful. "
        "[laugh]/[chuckle] and <emphasis> fit here when something's genuinely fun."
    ),
    "upbeat": (
        "The user sounds positive. Keep it light and warm — a natural, friendly upbeat "
        "register; a [chuckle] or <emphasis> where it genuinely fits."
    ),
    "neutral": (
        "Natural, warm conversational delivery — weave in a delivery tag only where it "
        "genuinely fits the moment."
    ),
}


def prosody_directive(emotion: Mapping[str, Any] | None) -> tuple[Register, str]:
    """The (register, generation-instruction) pair for this turn's emotional read."""
    register = read_register(emotion)
    return register, _DIRECTIVES[register]


def strip_inappropriate_tags(voice_text: str, register: Register) -> str:
    """Deterministic backstop: remove levity tags on a down/stressed turn so the voice
    never laughs when it shouldn't, even if the model slipped one in."""
    if register not in ("down", "stressed"):
        return voice_text
    cleaned = voice_text
    for tag in _LEVITY_TAGS:
        cleaned = cleaned.replace(tag, "").replace(tag.upper(), "")
    # Collapse any double spaces the removal introduced.
    while "  " in cleaned:
        cleaned = cleaned.replace("  ", " ")
    return cleaned.strip()


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
