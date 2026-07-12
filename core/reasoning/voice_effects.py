"""Voice-effect demos and whole-reply effect overrides (design §10.2 TTS/prosody).

Two demo-facing capabilities the normal reply path could not serve, because that
path picks delivery tags from the turn's *mood* and then a safety net strips
levity / caps length (`prosody.strip_inappropriate_tags`, the brevity enforcer):

1. **Demonstrate the effects on request.** "give me a few tone examples", "how many
   voice effects do you support", "show me different voice effects", "produce 5
   examples" → the companion speaks example sentences, each carrying a REAL Grok
   delivery tag, so a listener hears the range. Deterministic (no LLM, $0) so the
   effects are always present, correctly formed, and the requested COUNT is exact.

2. **Speak the whole next reply in a named effect.** "answer in a whisper", "say
   that slowly", "whisper this" → the entire reply is wrapped in that effect for TTS.

Every tag used here is in `response_gen._ALLOWED_TAGS`, so it survives `_sanitize_tags`
to the voice. The demo builds BOTH a clean labelled list (chat UI / memory, brief
§1.4) and a spoken form that announces each effect then performs it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

EffectKind = Literal["wrap", "lead"]
Category = Literal["wrapping", "instant"]


@dataclass(frozen=True)
class VoiceEffect:
    """One demonstrable Grok delivery effect.

    ``kind`` decides how the effect is applied to arbitrary text for an override:
    ``wrap`` surrounds a span (``<whisper>…</whisper>``); ``lead`` prepends a
    point/tone tag (``[laugh] …``). ``category`` is how Grok groups it —
    ``wrapping`` (shape a whole span: volume, pitch/speed, vocal style) vs
    ``instant`` (fire at a point: pauses, laughter, mouth sounds, breathing).
    ``tag`` is the whitelisted tag word (see response_gen._ALLOWED_TAGS).
    """

    key: str
    kind: EffectKind
    category: Category
    tag: str
    label: str
    clean_example: str
    voice_example: str


# The COMPLETE set of Grok delivery effects the app performs — both WRAPPING tags
# (`<…>`, shape a span) and INSTANT tags (`[…]`, fire at a point) — grounded in
# xAI's documented tag categories (docs.x.ai audio/text-to-speech) and the live-
# validated whitelist in response_gen._ALLOWED_TAGS. Ordered for VARIETY first so a
# small "give me a couple" demo leads with the clearest contrasts; `build_demo`
# regroups by category for a full "what do you support" listing.
EFFECT_CATALOG: tuple[VoiceEffect, ...] = (
    # --- wrapping (shape a whole span) ---
    VoiceEffect(
        "whisper",
        "wrap",
        "wrapping",
        "whisper",
        "Whisper",
        "Come closer — I'll let you in on a little secret.",
        "<whisper>Come closer, I'll let you in on a little secret.</whisper>",
    ),
    VoiceEffect(
        "emphasis",
        "wrap",
        "wrapping",
        "emphasis",
        "Emphasis",
        "This part right here really matters.",
        "This part right here <emphasis>really</emphasis> matters.",
    ),
    VoiceEffect(
        "slow",
        "wrap",
        "wrapping",
        "slow",
        "Slow",
        "Let's just take this one nice and slow.",
        "<slow>Let's just take this one nice and slow.</slow>",
    ),
    VoiceEffect(
        "fast",
        "wrap",
        "wrapping",
        "fast",
        "Fast",
        "Okay okay — listen, listen, you have to hear this!",
        "<fast>Okay okay, listen, listen, you have to hear this!</fast>",
    ),
    VoiceEffect(
        "soft",
        "wrap",
        "wrapping",
        "soft",
        "Soft",
        "No rush at all — take all the time you need.",
        "<soft>No rush at all, take all the time you need.</soft>",
    ),
    VoiceEffect(
        "loud",
        "wrap",
        "wrapping",
        "loud",
        "Loud",
        "Get up — this is your moment, go get it!",
        "<loud>Get up, this is your moment, go get it!</loud>",
    ),
    VoiceEffect(
        "sing",
        "wrap",
        "wrapping",
        "sing",
        "Singing",
        "Happy birthday to you…",
        "<sing>Happy birthday to you…</sing>",
    ),
    # --- instant (fire at a point) ---
    VoiceEffect(
        "laugh",
        "lead",
        "instant",
        "laugh",
        "Laugh",
        "Okay, that is honestly hilarious.",
        "[laugh] Okay, that is honestly hilarious.",
    ),
    VoiceEffect(
        "chuckle",
        "lead",
        "instant",
        "chuckle",
        "Chuckle",
        "You're really too much, you know that?",
        "[chuckle] You're really too much, you know that?",
    ),
    VoiceEffect(
        "sigh",
        "lead",
        "instant",
        "sigh",
        "Sigh",
        "What a long, long day it has been.",
        "[sigh] What a long, long day it has been.",
    ),
    VoiceEffect(
        "gasp",
        "lead",
        "instant",
        "gasp",
        "Gasp",
        "No way — you actually did it?",
        "[gasp] No way, you actually did it?",
    ),
    VoiceEffect(
        "breath",
        "lead",
        "instant",
        "breath",
        "Breath",
        "Okay… let's begin.",
        "[breath] Okay. Let's begin.",
    ),
    VoiceEffect(
        "exhale",
        "lead",
        "instant",
        "exhale",
        "Exhale",
        "Alright — that's finally off my chest.",
        "[exhale] Alright, that's finally off my chest.",
    ),
    VoiceEffect(
        "sniff",
        "lead",
        "instant",
        "sniff",
        "Sniff",
        "That actually got to me a little.",
        "[sniff] That actually got to me a little.",
    ),
    VoiceEffect(
        "clears throat",
        "lead",
        "instant",
        "clears throat",
        "Clears throat",
        "Ahem — may I have your attention?",
        "[clears throat] Ahem, may I have your attention?",
    ),
    VoiceEffect(
        "pause",
        "lead",
        "instant",
        "pause",
        "Pause",
        "Wait for it… there it is.",
        "Wait for it <pause> there it is.",
    ),
    VoiceEffect(
        "gentle",
        "lead",
        "instant",
        "gentle",
        "Gentle",
        "Hey — it's alright. I've got you.",
        "[gentle] Hey, it's alright. I've got you.",
    ),
    VoiceEffect(
        "warm",
        "lead",
        "instant",
        "warm",
        "Warm",
        "It's genuinely good to hear your voice.",
        "[warm] It's genuinely good to hear your voice.",
    ),
)

_BY_KEY = {e.key: e for e in EFFECT_CATALOG}
DEFAULT_DEMO_COUNT = 4
MAX_DEMO_COUNT = len(EFFECT_CATALOG)


@dataclass(frozen=True)
class DemoRequest:
    """A request to hear/see the voice effects. ``count`` is how many to show."""

    count: int


# --- Demo detection -------------------------------------------------------------

# The subject a demo request talks about: the companion's expressive VOICE, not the
# world. Kept broad enough for the natural ways a user asks ("tone", "vibe", "delivery")
# but anchored to voice/effect vocabulary so a normal question can't trip it.
_DEMO_SUBJECT = re.compile(
    r"\b(voice|vocal|tone|tones|delivery|expression|expressions|expressive|inflection|"
    r"prosody|accent(?:s)?|sound effect(?:s)?|voice effect(?:s)?|audio effect(?:s)?|"
    r"vibe(?:s)?)\b",
    re.IGNORECASE,
)
# The word "effect(s)" on its own also qualifies as the subject (e.g. "show me some
# effects", "how many effects do you support") — common phrasing that omits "voice".
_EFFECT_WORD = re.compile(r"\beffects?\b", re.IGNORECASE)
# A demo VERB / capability question: the user wants to see or hear the range.
_DEMO_CUE = re.compile(
    r"\b(example|examples|demo|demonstrate|showcase|show(?:\s+me)?|give(?:\s+me)?|"
    r"produce|generate|list|hear|sample|samples|what\s+kind|what\s+sort|"
    r"how\s+many|how\s+much|which)\b",
    re.IGNORECASE,
)
# "what can you do / what your voice can do / effects you support|produce|can do" —
# a capability question phrased without an explicit demo verb.
_CAPABILITY = re.compile(
    r"\b(can\s+you\s+do|you\s+can\s+do|do\s+you\s+support|you\s+support|"
    r"can\s+you\s+produce|can\s+you\s+make|do\s+you\s+have|you\s+have|have\s+you\s+got|"
    r"are\s+(?:there|available))\b",
    re.IGNORECASE,
)

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "a couple": 2,
    "couple": 2,
    "a pair": 2,
}
_DIGITS = re.compile(r"\b(\d{1,2})\b")
_NUMWORD = re.compile(
    r"\b(a couple|couple|a pair|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve)\b",
    re.IGNORECASE,
)
_ALL = re.compile(r"\b(all|every|each|the full|full range|whole|complete|entire)\b", re.IGNORECASE)
# A coverage question ("how many do you support", "what kinds are there", "list them")
# wants the FULL range, not a small sample — so it defaults to every effect.
_COVERAGE = re.compile(
    r"\b(how many|how much|what kind|what kinds|what sort|what all|which|list|"
    r"do you support|you support|do you have|can you do|are there)\b",
    re.IGNORECASE,
)


def _extract_count(text: str) -> int:
    """The requested number of examples, clamped to the catalogue. An explicit
    number wins; a coverage question ("how many do you support") or "all" shows
    every effect; a vague ask ("a few", "some", "examples") shows DEFAULT_DEMO_COUNT."""
    m = _DIGITS.search(text)
    if m:
        return max(1, min(int(m.group(1)), MAX_DEMO_COUNT))
    w = _NUMWORD.search(text)
    if w:
        return max(1, min(_NUMBER_WORDS[w.group(1).lower()], MAX_DEMO_COUNT))
    if _ALL.search(text) or _COVERAGE.search(text):
        return MAX_DEMO_COUNT
    return DEFAULT_DEMO_COUNT


def detect_demo_request(utterance: str | None) -> DemoRequest | None:
    """True when the user is asking to hear/see the companion's voice effects.

    Requires BOTH a voice/effect subject and a demo verb or capability question, so
    ordinary turns ("what's the weather", "I love your voice") do not trigger a demo.
    """
    text = (utterance or "").strip()
    if not text:
        return None
    has_subject = bool(_DEMO_SUBJECT.search(text) or _EFFECT_WORD.search(text))
    if not has_subject:
        return None
    if not (_DEMO_CUE.search(text) or _CAPABILITY.search(text)):
        return None
    return DemoRequest(count=_extract_count(text))


# --- Whole-reply effect override ------------------------------------------------

# Effect phrasings the user might name, mapped to a catalogue key. Includes adverbs
# and synonyms; the first match (longest patterns first) wins.
_OVERRIDE_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(whisper(?:ed|ing|s)?|hushed|under your breath)\b", re.I), "whisper"),
    (
        re.compile(r"\b(slow(?:ly)?|slow motion|slowed down|drawn[- ]out|draw it out)\b", re.I),
        "slow",
    ),
    (
        re.compile(
            r"\b(fast(?:er)?|quick(?:ly)?|rapidly|excited(?:ly)?|energetic(?:ally)?)\b", re.I
        ),
        "fast",
    ),
    (
        re.compile(
            r"\b(emphasi[sz]e(?:d)?|emphatic(?:ally)?|with emphasis|forceful(?:ly)?"
            r"|strong(?:ly)?)\b",
            re.I,
        ),
        "emphasis",
    ),
    (re.compile(r"\b(loud(?:ly)?|shout(?:ing)?|yell(?:ing)?)\b", re.I), "loud"),
    (re.compile(r"\b(sing(?:ing)?|in song|like a song)\b", re.I), "sing"),
    (re.compile(r"\b(gentl(?:e|y)|tender(?:ly)?|soothing(?:ly)?)\b", re.I), "gentle"),
    (re.compile(r"\b(soft(?:ly)?|quiet(?:ly)?)\b", re.I), "soft"),
    (re.compile(r"\b(warm(?:ly)?)\b", re.I), "warm"),
    (re.compile(r"\b(laugh(?:ing)?|while laughing|through a laugh)\b", re.I), "laugh"),
    (re.compile(r"\b(sigh(?:ing)?|with a sigh)\b", re.I), "sigh"),
    (re.compile(r"\b(chuckl(?:e|ing))\b", re.I), "chuckle"),
    (re.compile(r"\b(sad(?:ly)?|mournful(?:ly)?|somber(?:ly)?)\b", re.I), "gentle"),
)

# The request must be ADDRESSED to the companion — an imperative or a "can you …"
# — so "he said it slowly and I got scared" (a third-person narration) is not an
# override. One of these cues must be present alongside an effect phrase.
_REQUEST_CUE = re.compile(
    r"\b(say|read|reply|respond|answer|tell me|speak|talk|do it|do this|do that|"
    r"give me|put it|make it|make this|make your|can you|could you|would you|will you|"
    r"please|in a|in an|next (?:response|reply|answer|one)|"
    r"this (?:response|reply|answer|one|time)|that (?:response|reply|answer|one)|"
    r"your (?:next |whole )?(?:response|reply|answer|voice))\b",
    re.IGNORECASE,
)


def detect_effect_override(utterance: str | None) -> str | None:
    """Return a catalogue key when the user asks for the WHOLE reply in a named
    effect ("answer in a whisper", "say that slowly"), else None.

    Deliberately conservative: it requires a request cue directed at the companion
    (imperative / "can you …" / "in a …") so ordinary sentences that merely contain
    an effect word ("I whispered to her") do not hijack the delivery.
    """
    text = (utterance or "").strip()
    if not text:
        return None
    if not _REQUEST_CUE.search(text):
        return None
    for pattern, key in _OVERRIDE_PHRASES:
        if pattern.search(text):
            return key
    return None


def apply_effect_override(voice_text: str, key: str) -> str:
    """Wrap/prepend ``voice_text`` so the whole reply is spoken in the given effect.

    ``wrap`` effects surround the (already tag-free) core; ``lead`` effects prepend
    the point/tone tag. If the text already opens with the same effect, it is left
    as-is (no double tag). Returns text unchanged for an unknown key.
    """
    effect = _BY_KEY.get(key)
    core = (voice_text or "").strip()
    if effect is None or not core:
        return voice_text
    if effect.kind == "wrap":
        open_tag, close_tag = f"<{effect.tag}>", f"</{effect.tag}>"
        if core.startswith(open_tag):
            return core
        return f"{open_tag}{core}{close_tag}"
    lead = f"[{effect.tag}]" if effect.tag != "pause" else "<pause>"
    if core.startswith(lead):
        return core
    return f"{lead} {core}"


def _variety_order(effects: tuple[VoiceEffect, ...]) -> list[VoiceEffect]:
    """Interleave wrapping and instant effects so a small demo mixes both kinds
    (a bare ``effects[:n]`` would otherwise hand back N wrapping effects in a row)."""
    wrapping = [e for e in effects if e.category == "wrapping"]
    instant = [e for e in effects if e.category == "instant"]
    order: list[VoiceEffect] = []
    for w, i in zip(wrapping, instant, strict=False):
        order.extend((w, i))
    longer = wrapping if len(wrapping) > len(instant) else instant
    order.extend(longer[min(len(wrapping), len(instant)) :])
    return order


def build_demo(count: int, effects: tuple[VoiceEffect, ...] = EFFECT_CATALOG) -> tuple[str, str]:
    """Build (display_text, voice_text) for a voice-effect demo.

    ``display_text`` is a clean numbered list for the chat UI / memory (no tags,
    brief §1.4), each line annotated with its category (wrapping vs instant) so the
    full range is legible. ``voice_text`` announces each effect by name then PERFORMS
    it with the real tag, so the listener hears the label and the effect back to back.
    The chosen effects interleave both kinds, so even a short demo shows the breadth.
    """
    n = max(1, min(count, len(effects)))
    chosen = _variety_order(effects)[:n]

    intro = "Sure — here's a taste of what my voice can do:"
    display_lines = [intro]
    for i, e in enumerate(chosen, 1):
        display_lines.append(f'{i}. {e.label} ({e.category}) — "{e.clean_example}"')
    display = "\n".join(display_lines)

    voice_parts = ["Sure, here's a taste of what my voice can do."]
    for e in chosen:
        # Announce in a natural voice, then perform the tagged example.
        voice_parts.append(f"Here's {e.label.lower()}. {e.voice_example}")
    voice = " ".join(voice_parts)
    return display, voice
