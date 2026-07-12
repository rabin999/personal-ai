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

# Every spoken pool carries occasional DELIVERY TAGS (whitelisted in response_gen._ALLOWED_TAGS)
# so the beat is PERFORMED, not read flat — a gentle tone on a hard moment, a warm one when
# receiving, a small pause where a friend would actually breathe. Tags are chosen to fit each
# pool's mood: never levity ([chuckle]) on empathy/apology; warmth/pauses everywhere.

ACK_EMPATHY = (
    "[gentle] Oh no… <pause> that's a lot.",
    "[soft] Ugh, I'm really sorry you're dealing with that.",
    "[gentle] Oh, that sounds genuinely rough.",
    "[soft] Aw… <pause> hey, I'm right here.",
    "[sigh] Oh man. That's a heavy one.",
    "[gentle] That sounds like a lot to carry right now.",
    "[soft] Oof… I hear you. That's tough.",
    "[gentle] Hey — take a breath. I've got you.",
)
ACK_LOOKUP = (
    "[warm] On it — let me pull that up.",
    "Alright, one sec while I check the latest on that.",
    "[warm] Good one — let me look that up properly.",
    "Hang on, I'm checking on that right now.",
    "Let me dig into that <pause> give me a beat.",
    "[warm] Okay, let me go find that out.",
    "One moment — pulling the current details together.",
    "Let me go check that so I get it right.",
)
ACK_THINKING = (
    "[warm] Hmm <pause> let me actually think on that.",
    "Ooh, good question — give me a second here.",
    "Let me chew on that one for a beat.",
    "[warm] Hang on, I want to think this through with you.",
    "That's a real one — <pause> let me sit with it.",
    "Mm, let me turn that over for a sec.",
)
# Warm BACKCHANNELS the instant the user finishes a SHORT statement — the "I'm listening, go on"
# beat a friend gives while taking in what you said, so the reply never lands on dead silence.
ACK_BACKCHANNEL = (
    "[warm] Mm, gotcha.",
    "Yeah, I hear you.",
    "[warm] Oh, okay — I'm with you.",
    "Right, I'm following.",
    "Mm, that makes sense.",
    "[warm] Ah, I see.",
    "Yeah, for sure.",
    "Okay <pause> go on.",
)
# The companion leans IN on a meatier statement the user shares (not a question, not distress) —
# genuinely curious, inviting them to keep going rather than just receiving it.
ACK_INTEREST = (
    "[warm] Oh, interesting — tell me more.",
    "Ooh, okay — I'm listening.",
    "[warm] Huh, that's got my attention. Go on.",
    "Wait, really? Say more about that.",
    "[warm] Mm, I want to hear the rest of this.",
    "Okay, that's a good one — keep going.",
)
# Warm, gracious beats when the user thanks the companion — never a stiff "you're welcome".
ACK_GRATITUDE = (
    "[warm] Aw, of course — anytime.",
    "[chuckle] Hey, that's what I'm here for.",
    "[warm] Anytime, seriously.",
    "Course — happy to.",
    "[warm] You got it.",
    "[chuckle] Don't mention it — really.",
)
ACK_RECALL = (
    "[warm] Let me look back through our chats a sec.",
    "One moment — let me remember what we talked about.",
    "[warm] Hmm <pause> let me think back on that.",
    "Give me a beat to dig through our conversation.",
    "Let me pull up what we went over before.",
    "[warm] One sec — checking back over our chats.",
)
PROGRESS_LOOKUP = (
    "[warm] Still on it — almost there.",
    "Still digging <pause> one more second.",
    "[warm] Still pulling it together, hang tight.",
    "Nearly there — just gathering the last details.",
    "Still searching — this won't be long.",
    "Just double-checking a couple more sources.",
)
PROGRESS_THINKING = (
    "[warm] Still thinking it through.",
    "Almost got it — one more beat.",
    "Still piecing it together <pause> hang tight.",
    "[warm] Nearly there, promise.",
    "Still working it out — won't be long.",
)
PROGRESS_APOLOGY = (
    "[soft] Sorry, this is taking longer than I expected — still on it.",
    "[gentle] Apologies for the wait — I'm digging as fast as I can.",
    "[soft] So sorry it's taking a while <pause> I want to get this right.",
    "[gentle] Sorry to keep you waiting — almost there, I promise.",
    "[soft] Sorry for the wait — I'm really trying to pin this down.",
    "[gentle] Still going — sorry it's slow, I want to get it right for you.",
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
        "warm and brief, meeting the feeling, never fixing or advising. Use a gentle/soft "
        "delivery tag like [gentle] [soft] [sigh] or a <pause>; NEVER [laugh]/[chuckle]",
        max_words=12,
        min_lines=4,
    ),
    PoolSpec(
        "ack_lookup",
        "instant one-liners said the moment the companion starts looking something up for the "
        "user (a web/live lookup) — brisk, warm, casual; commit to NO facts. A [warm] tag or a "
        "<pause> fits; no levity",
        max_words=13,
        min_lines=4,
    ),
    PoolSpec(
        "ack_thinking",
        "instant one-liners when the companion needs a beat to think about a non-lookup "
        "question — casual, unhurried; commit to NO facts. A [warm] tag, a <slow>…</slow> or a "
        "<pause> fits the thinking beat",
        max_words=13,
        min_lines=4,
    ),
    PoolSpec(
        "ack_backchannel",
        "short, warm backchannels the instant the user finishes a SHORT statement (not a "
        "question) — the natural 'I'm listening, go on' beat a friend gives, receiving what they "
        "said; NOT 'let me think'; commit to NO facts. A [warm] tag or a <pause> fits",
        max_words=8,
        min_lines=5,
    ),
    PoolSpec(
        "ack_interest",
        "warm, genuinely curious beats when the user shares a MEATIER statement (not a question, "
        "not distress) — lean in and invite them to keep going ('oh, interesting — tell me "
        "more'); commit to NO facts. A [warm] tag fits",
        max_words=10,
        min_lines=4,
    ),
    PoolSpec(
        "ack_gratitude",
        "warm, gracious beats when the user THANKS the companion — never a stiff 'you're "
        "welcome'; easy and close ('aw, of course — anytime'). A [warm] or [chuckle] tag fits",
        max_words=8,
        min_lines=4,
    ),
    PoolSpec(
        "ack_recall",
        "instant one-liners when the beat is spent recalling PAST conversations with the user "
        "(not a web lookup) — nods to remembering/looking back through your chats. A [warm] tag "
        "or a <pause> fits",
        max_words=14,
        min_lines=4,
    ),
    PoolSpec(
        "progress_lookup",
        "brief progress nudges said while a slow search is STILL running, after the first "
        "interjection — reassure it's still happening; commit to NO facts. A [warm] tag or a "
        "<pause> fits",
        max_words=14,
        min_lines=3,
    ),
    PoolSpec(
        "progress_thinking",
        "brief progress nudges while STILL thinking on a slow non-lookup turn — reassure "
        "you're still on it; commit to NO facts. A [warm] tag or a <pause> fits",
        max_words=14,
        min_lines=3,
    ),
    PoolSpec(
        "progress_apology",
        "gentle apologies when the wait has really dragged on — sorry it's taking longer than "
        "expected, still trying your best; warm, humble, brief. A [soft]/[gentle] tag or a "
        "<pause> fits; NEVER levity",
        max_words=18,
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
    "ack_backchannel": ACK_BACKCHANNEL,
    "ack_interest": ACK_INTEREST,
    "ack_gratitude": ACK_GRATITUDE,
    "ack_recall": ACK_RECALL,
    "progress_lookup": PROGRESS_LOOKUP,
    "progress_thinking": PROGRESS_THINKING,
    "progress_apology": PROGRESS_APOLOGY,
    "greeting_angles": GREETING_ANGLES,
}
