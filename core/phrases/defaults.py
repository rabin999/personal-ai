"""Canonical default phrase pools + their regeneration specs.

This is the single source of the hand-written spoken one-liners. `core/reasoning/response_gen`
and `voice/session` read these (via the `PhraseCatalog`) rather than defining their own copies,
so the defaults, the live reads, and the background regenerator all agree on the pool names and
intents. Every default line here has been checked against the forbidden-assistant-speak scrubber
(`core.reasoning.style`) so the fallback is always safe to speak.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PoolSpec:
    """What a pool is, so the background regenerator can rewrite it in the same spirit and the
    validator can judge whether a regenerated line is acceptable."""

    name: str
    intent: str  # a one-line brief handed to the regenerator LLM
    max_words: int  # a spoken filler must stay short
    min_lines: int  # below this after validation → keep the default pool, don't ship a thin one
    spoken: bool = True  # spoken one-liners are scrubber-checked; greeting ANGLES are directives


# ── the spoken one-liners (interjections + progress nudges) ──────────────────────────────

ACK_EMPATHY = (
    "Oh no…",
    "Ugh, that's a lot.",
    "Oh, that's really rough.",
    "Aw, I'm sorry.",
    "Oh man…",
    "That sounds heavy.",
    "Oof, that's tough.",
    "Hey — that's a lot to carry.",
)
ACK_LOOKUP = (
    "On it — let me check.",
    "One sec, pulling that up.",
    "Let me look that up.",
    "Hang on, checking now.",
    "Give me a sec to find that.",
    "Let me dig into that.",
    "Alright, let me see.",
    "Checking on that right now.",
)
ACK_THINKING = (
    "Hmm, let me think.",
    "Ooh, good one — one sec.",
    "Let me chew on that.",
    "Hang on, thinking it over.",
    "Good question — give me a beat.",
    "Let me sit with that a sec.",
)
ACK_RECALL = (
    "Let me look back through our chats.",
    "One sec, let me remember what we talked about.",
    "Hmm, let me think back.",
    "Give me a sec to dig through our conversation.",
    "Let me pull up what we discussed.",
    "One moment — checking back over our chats.",
)
PROGRESS_LOOKUP = (
    "Still on it — almost there.",
    "Still digging, one more sec.",
    "Still pulling it together — hang tight.",
    "Nearly there, still gathering the details.",
    "Still searching — won't be long.",
    "Just checking a couple more sources.",
)
PROGRESS_THINKING = (
    "Still thinking it through.",
    "Almost got it — one more beat.",
    "Still piecing it together.",
    "Hang tight, nearly there.",
    "Still working it out — won't be long.",
)
PROGRESS_APOLOGY = (
    "Sorry, this is taking longer than I expected — still on it.",
    "Apologies for the wait — I'm digging as fast as I can.",
    "So sorry it's taking a while — I want to get this right.",
    "Sorry to keep you waiting — almost there, I promise.",
    "Sorry for the wait — I'm trying my best to pin this down.",
    "Still going — sorry it's slow, I want to get it right for you.",
)

# ── the greeting ANGLES (stage directions for the open-greeting LLM, not spoken verbatim) ──

GREETING_ANGLES = (
    "if you GENUINELY know their local time of day, you can nod to it naturally — but "
    "ONLY if the prompt actually tells you their local time; never guess morning/evening",
    "riff lightly on how long it's been since you last talked",
    "just an easy, plain 'hey' with their name — nothing extra",
    "be a little playful or teasing",
    "sound genuinely glad they showed up, warm but brief",
    "open low-key and breezy, like catching up mid-thought",
    "pick up on the vibe of the moment and keep it casual",
    "a short, curious 'hey, you' kind of energy",
)


POOL_SPECS: tuple[PoolSpec, ...] = (
    PoolSpec(
        "ack_empathy",
        "instant, gentle reactions the moment the user shares something hard or painful — "
        "warm and brief, meeting the feeling, never fixing or advising",
        max_words=8,
        min_lines=4,
    ),
    PoolSpec(
        "ack_lookup",
        "instant one-liners said the moment the companion starts looking something up for the "
        "user (a web/live lookup) — brisk, warm, casual; commit to NO facts",
        max_words=8,
        min_lines=4,
    ),
    PoolSpec(
        "ack_thinking",
        "instant one-liners when the companion needs a beat to think about a non-lookup "
        "question — casual, unhurried; commit to NO facts",
        max_words=8,
        min_lines=4,
    ),
    PoolSpec(
        "ack_recall",
        "instant one-liners when the beat is spent recalling PAST conversations with the user "
        "(not a web lookup) — nods to remembering/looking back through your chats",
        max_words=10,
        min_lines=4,
    ),
    PoolSpec(
        "progress_lookup",
        "brief progress nudges said while a slow search is STILL running, after the first "
        "interjection — reassure it's still happening; commit to NO facts",
        max_words=9,
        min_lines=3,
    ),
    PoolSpec(
        "progress_thinking",
        "brief progress nudges while STILL thinking on a slow non-lookup turn — reassure "
        "you're still on it; commit to NO facts",
        max_words=9,
        min_lines=3,
    ),
    PoolSpec(
        "progress_apology",
        "gentle apologies when the wait has really dragged on — sorry it's taking longer than "
        "expected, still trying your best; warm, humble, brief",
        max_words=14,
        min_lines=3,
    ),
    PoolSpec(
        "greeting_angles",
        "stage directions (NOT spoken verbatim) for how to angle a warm, brief spoken hello "
        "when the user opens the app — each a short instruction for varying the greeting's vibe",
        max_words=30,
        min_lines=4,
        spoken=False,
    ),
)

DEFAULT_POOLS: dict[str, tuple[str, ...]] = {
    "ack_empathy": ACK_EMPATHY,
    "ack_lookup": ACK_LOOKUP,
    "ack_thinking": ACK_THINKING,
    "ack_recall": ACK_RECALL,
    "progress_lookup": PROGRESS_LOOKUP,
    "progress_thinking": PROGRESS_THINKING,
    "progress_apology": PROGRESS_APOLOGY,
    "greeting_angles": GREETING_ANGLES,
}
