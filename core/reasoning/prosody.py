"""Dynamic prosody selection (brief U8; design §10.2, §3.6.5).

The companion's voice must not be flat, and must not be MISMATCHED (the reported
bug: laughing on a sad turn). This maps the per-turn emotional read to (1) an explicit
register directive injected into generation so the model weaves the RIGHT delivery tags,
and (2) a deterministic backstop that strips clearly-inappropriate tags (a laugh on a
down/stressed turn) before TTS — the model's guidance plus a safety net.

**Two sources, in precedence order:**
1. **Acoustic SER** (emotion2vec, §22) — needs a GPU service. When `ser_service_url` is
   empty it yields nothing.
2. **Text sentiment** — `emotion_from_text()`, derived from the reasoning step's own
   `emotional_read` (the context/intent node already infers it from the transcript and
   the conversation; it used to be logged and discarded).

Source 2 is the FLOOR, not a replacement for source 1. Until it existed, `ser_service_url`
was empty in every deployment, so `prompt.emotion` was always `None`, `read_register()`
always returned `"neutral"`, and the whole dynamic-prosody system — marked ✅ — had never
once executed on a real turn. Three docstrings claimed a "falls back to text-sentiment"
behaviour that had no implementation behind it.

Emotion is a probabilistic signal, never a diagnosis (§22 rule 4): a low-confidence
read defaults to a natural register rather than forcing a tone.
"""

from __future__ import annotations

import re
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


# Free-text emotional reads → the categorical labels `read_register` understands, with
# dimensional values so a downstream consumer sees a complete `EmotionRead`-shaped dict.
# Ordered: the first family that matches wins, so "frustrated and sad" reads as stressed.
_TEXT_EMOTIONS: tuple[tuple[re.Pattern[str], dict[str, Any]], ...] = (
    (
        re.compile(
            r"frustrat|angry|anger|annoy|irritat|impatien|stress|anxious|anxiety|worried|"
            r"worry|nervous|tense|overwhelm|afraid|scared|fear|agitat|urgent|blunt|curt",
            re.IGNORECASE,
        ),
        {"label": "frustrated", "valence": -0.4, "arousal": 0.6},
    ),
    (
        re.compile(
            r"\bsad\b|sadness|down|low\b|grief|griev|lonely|loneli|hurt|upset|heartbroken|"
            r"disappoint|despair|depress|tired|exhaust|weary|numb|empty|mourning|loss|"
            # D-5, other half: the design doc's own flagship indirect-emotional scenario
            # ("what's happening in Nepal … gives me a lot of pain") produces the read
            # "pain", which matched NOTHING here — `hurt` was listed, `pain` was not. The
            # most emotionally loaded turn in the golden set was delivered neutrally.
            r"pain|ache|aching|anguish|distress|sorrow|devastat|bereav|miss (?:him|her|them)",
            re.IGNORECASE,
        ),
        {"label": "sad", "valence": -0.5, "arousal": 0.2},
    ),
    (
        re.compile(
            r"excit|thrill|elat|ecstat|overjoy|delight|euphor|pumped|buzz|proud|celebrat",
            re.IGNORECASE,
        ),
        {"label": "excited", "valence": 0.6, "arousal": 0.7},
    ),
    (
        re.compile(r"happy|happi|glad|pleased|content|cheer|good mood|positive|warm|hopeful",
                   re.IGNORECASE),
        {"label": "happy", "valence": 0.4, "arousal": 0.2},
    ),
)  # fmt: skip

# A text-derived read is a weaker signal than an acoustic one, but well above the
# `_MIN_CONFIDENCE` floor — it comes from a model that read the whole utterance.
_TEXT_CONFIDENCE = 0.6


# D-5. The context prompt asks the model for `"emotional_read": "<the feeling, or empty>"`, so
# on a neutral turn it writes the literal word **"empty"** — and `empty` is in the SAD family
# above, for "I feel empty". Result: `"what's 15% of 240?"` was delivered in a `down` register,
# 3 runs of 3. The prompt and the parser disagreed about their shared vocabulary and nothing
# checked. These are the ways a model says "no feeling here"; they are matched on the WHOLE
# read, so "empty, hollow, like nothing matters" still reads as sadness.
_NEUTRAL_READS = frozenset(
    {
        "", "empty", "none", "neutral", "calm", "n/a", "na", "-", "nothing", "nil",
        "no feeling", "no strong feeling", "no clear feeling", "not applicable",
        "unclear", "unknown", "no emotion", "no particular emotion", "flat",
    }
)  # fmt: skip


def emotion_from_text(emotional_read: str | None) -> dict[str, Any] | None:
    """Turn the reasoning step's free-text emotional read into an `EmotionRead`-shaped
    dict, or None when it is empty/neutral (never force a tone off a blank signal).

    This is the text-sentiment fallback the docstrings promised and nobody implemented.
    """
    text = (emotional_read or "").strip()
    if text.lower().strip(" .\"'") in _NEUTRAL_READS:
        return None
    for pattern, read in _TEXT_EMOTIONS:
        if pattern.search(text):
            return {**read, "confidence": _TEXT_CONFIDENCE, "source": "text"}
    return None


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
