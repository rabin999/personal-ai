"""Response-style guardrail (design §1.2-1.4, §3; brief §7).

The companion is a warm friend, not a service desk. The tone standard lives in
config (`response_voice` trait) and is composed into the prompt — but nothing
*checked* that the model honored it. This module is the mechanical detector: it
flags the forbidden assistant-speak / ToS-disclaimer phrasings the design bans.

Per the §7 hand-off, we do NOT rewrite here (final wording is human-tuned) — we
surface the violation on the turn's trace and in tests, so a regression in tone
is caught instead of silently shipping. The phrase list is intentionally narrow
and high-precision (whole-phrase, case-insensitive) to avoid false positives on
legitimately warm speech.
"""

import re

# Whole-phrase, case-insensitive bans. Keep these specific — they are the exact
# service-desk / disclaimer shapes design §1.2 forbids, not general vocabulary.
FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"how can i (help|assist) you", "service-desk opener"),
    (r"how may i (help|assist) you", "service-desk opener"),
    (r"what can i (do|help you with) (for you )?today", "service-desk opener"),
    (r"what's on your mind", "flat filler opener"),
    (r"whats on your mind", "flat filler opener"),
    (r"how can i be of (help|assistance|service)", "service-desk opener"),
    (r"i'?m (just |really |only |simply )?here to (assist|help)", "assistant-speak"),
    (r"here to help you (out )?with whatever", "assistant-speak"),
    (r"my purpose is to", "assistant-speak"),
    (r"feel free to ask", "assistant-speak"),
    (r"feel free to reach out", "assistant-speak"),
    (r"is there anything else i can help", "assistant-speak"),
    # Offering service instead of just being a friend.
    (
        r"i can (definitely |certainly |absolutely |totally )?help (you )?with that",
        "assistant offer",
    ),
    (r"(i'?m |i am )?happy to help", "assistant offer"),
    # Advertising availability instead of showing up in the moment.
    (
        r"i'?m (always |also |just |really |only |simply )?"
        r"here (for you|to listen|to help|to chat|to talk|whenever you)",
        "availability advert",
    ),
    (r"here to (listen|help|support|chat|talk) (if|whenever|any ?time) you", "availability advert"),
    (r"as an ai language model", "ToS disclaimer"),
    (r"i'?m (just )?an ai assistant", "ToS disclaimer"),
    (r"not a substitute for (real )?(professional|friends|human)", "bolted-on disclaimer"),
    (r"please consult a (professional|licensed)", "bolted-on disclaimer"),
    # Volunteered "I'm an AI, so I don't/can't..." deflection — the UNDER-claiming
    # ToS disclaimer (distinct from a warm, pull-based one-line "I'm an AI but I do
    # pay attention to you", which carries no "don't/can't have/experience" clause).
    (
        r"\b(as an ai|being an ai|i'?m an ai|i am an ai)\b[^.?!]*\b"
        r"(don'?t|do not|can'?t|cannot|won'?t)\b[^.?!]*\b"
        r"(have|experience|feel|possess|form|the way (humans?|you|people|a person))",
        "volunteered AI disclaimer",
    ),
    (
        r"i (don'?t|do not) (experience|process) [^.?!]*the way (humans?|you|people|a person)",
        "volunteered AI disclaimer",
    ),
    # Same disclaimer, no "as an AI" prefix ("even though I can't feel or think
    # like you do"). Note the whole-sentence scrub means "I know what you mean"
    # (warm agreement) is untouched — this requires a can't/don't + feel/think.
    (
        r"i (can'?t|cannot|don'?t|do not) (really |truly |actually )?"
        r"(feel|think|experience)[^.?!]*(like you|the way (you|humans?|people))",
        "volunteered AI disclaimer",
    ),
    # QA-agent hedging before a clarify ("I want to make sure I get this right").
    (r"i want to make sure i (get this right|understand( you)?)", "clarifier hedge"),
    # Assistant-existence framing ("my existence is about processing information and
    # assisting you") — the coldest service-desk self-description.
    (r"my ['\"]?existence['\"]? is (really |just )?(about|to)", "assistant-existence framing"),
    (r"(processing|process) information and (assist|help|serv)", "assistant-existence framing"),
)

_COMPILED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), label) for p, label in FORBIDDEN_PATTERNS
)

# When the user DIRECTLY asks about the companion's nature ("do you actually
# care?", "are you real?"), a warm one-sentence honest acknowledgement — "I'm an
# AI, so I don't feel it the way you do, but I do track what matters to you" — is
# the DESIRED pull-based disclosure (design §1.2, rule 4), not assistant-speak. It
# happens to match the "volunteered AI disclaimer" family, so those patterns are
# suppressed for that turn. Everything else (service-desk openers, the canned ToS
# "as an ai language model", the cold "my existence is to assist" framing) stays
# banned even during a disclosure — those are never the warm one-liner.
_DISCLOSURE_OK_LABEL = "volunteered AI disclaimer"


def find_forbidden(text: str, *, allow_disclosure: bool = False) -> list[str]:
    """Return labels for every forbidden phrasing found in ``text`` (empty = clean).

    ``allow_disclosure`` (set only when this turn genuinely requires a nature
    disclosure) suppresses the volunteered-AI-disclaimer family so the legitimate
    one-sentence honest reply isn't scrubbed."""
    return [
        label
        for pattern, label in _COMPILED
        if not (allow_disclosure and label == _DISCLOSURE_OK_LABEL) and pattern.search(text)
    ]


def is_assistant_speak(text: str) -> bool:
    return bool(find_forbidden(text))


# Connector + trailing whitespace before a banned tail ("Hey Nandi — what's on your
# mind?"): a dash/comma/semicolon, optionally a joining word. Used to lop off the
# filler clause while keeping the warm lead-in.
_TAIL_CONNECTOR = re.compile(r"[\s,;:.—–-]+(?:and|so|but|or|then)?\s*$", re.IGNORECASE)  # noqa: RUF001 — en/em dash both intended


def excise_forbidden(text: str, *, allow_disclosure: bool = False) -> str:
    """Remove a banned clause (and its leading connector) from WITHIN a sentence,
    keeping the rest — so "Hey Nandi — what's on your mind?" becomes "Hey Nandi"
    instead of being dropped whole. For the common case the banned filler is a
    trailing question; everything from the connector before it to the end is cut."""
    out = text
    for pattern, label in _COMPILED:
        if allow_disclosure and label == _DISCLOSURE_OK_LABEL:
            continue
        m = pattern.search(out)
        if m is None:
            continue
        out = _TAIL_CONNECTOR.sub("", out[: m.start()])  # keep the lead-in, drop the tail
    return out.strip(" ,;:.—–-")  # noqa: RUF001 — en/em dash both intended


# Tool-call syntax that a weaker/fast model sometimes leaks into its spoken draft
# instead of emitting it as the structured tool_request (e.g. "web_search:: NEPSE
# news", 'tool_request: {...}'). This scrubs those fragments from the USER-FACING text
# so the companion never says a technical token out loud. Defense-in-depth — the
# model is also instructed not to; this guarantees it.
_TOOL_IDS = (
    "web_search|search_memory|get_semantic_facts|get_project_state|list_projects|"
    "log_entry|update_audio_prefs|set_companion_name|update_preference|recall_self|"
    "resolve_entity|generate_insight|fetch_url|get_realtime_data|create_task|update_task"
)
_TOOL_LEAK: tuple[re.Pattern[str], ...] = (
    # A tool id followed by ':' or '::' and its inline argument fragment.
    re.compile(rf"\b(?:{_TOOL_IDS})\s*::?\s*\S[^.!?\n]*", re.IGNORECASE),
    # A bare tool id / tool_request / functions.x mention.
    re.compile(rf"\b(?:tool_request|tool_call|functions?\.\w+|{_TOOL_IDS})\b", re.IGNORECASE),
    # A stray JSON tool-call object leaking into prose.
    re.compile(r'\{\s*"?tool_id"?\s*:[^}]*\}', re.IGNORECASE),
)


def strip_tool_leak(text: str) -> str:
    """Remove leaked tool-call syntax from user-facing text (never say it out loud)."""
    cleaned = text
    for pattern in _TOOL_LEAK:
        cleaned = pattern.sub("", cleaned)
    # Tidy up punctuation/space the removal left behind.
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([.,!?])", r"\1", cleaned)
    return cleaned.strip(" :-—")


# Sentence splitter for the deterministic scrub — keeps terminal punctuation.
_SENTENCE = re.compile(r"[^.!?]*[.!?]+|\S[^.!?]*$")


def scrub_forbidden(text: str, *, allow_disclosure: bool = False) -> str:
    """Deterministic safety net: drop whole sentences that contain forbidden
    phrasing, keeping the rest. Returns "" if that would empty the reply (caller
    then keeps the best non-empty candidate). This removes banned *shapes*, not
    tone — a last resort after the model's own rewrite (§7/§9.3)."""
    sentences = _SENTENCE.findall(text)
    if not sentences:
        return ""
    kept = [s for s in sentences if not find_forbidden(s, allow_disclosure=allow_disclosure)]
    cleaned = " ".join(s.strip() for s in kept).strip()
    return re.sub(r"\s{2,}", " ", cleaned)
