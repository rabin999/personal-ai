"""Response-style guardrail (design §1.2-1.4, §3; brief §7).

The companion is a warm friend, not a service desk. The tone standard lives in
config (`response_voice` trait) and is composed into the prompt — but nothing
*checked* that the model honored it. This module is the mechanical detector: it
flags the forbidden assistant-speak / ToS-disclaimer phrasings the design bans.

It is also the TRIGGER for self-reflection (§9.3): `_finalize` only runs the LLM
rewrite when this module flags something. So a detector that misses is a
self-reflection step that never fires.

**Calibrated against the LLM-judge, not intuition.** The first judged baseline of the
live voice path (`docs/quality/baseline_live.json`) had the judge marking 3 of 11
scenarios `chatbot_like` while this module flagged **zero of them** — and it missed all
three of the project's own curated `gs3_judge.json` negative examples too. The bar is:
*if the judge marks a reply chatbot_like, the detector must flag it*, without flagging
replies the judge passes. `scripts/style_calibration.py` measures that agreement; run it
after touching this file.

Rules of thumb learned from that calibration:
- "sorry **to hear** that" is warm empathy; "sorry **for/about/if**" is a service apology.
- "I'm here to listen" on a grief turn is warm; it only becomes an availability *advert*
  with a qualifier ("always", "whenever you need", "any time").
- Length does not separate good from bad (good replies run to 67 words, bad to 70), so
  there is deliberately no word-count rule.
"""

import re

# Whole-phrase, case-insensitive bans. Keep these specific — they are the exact
# service-desk / disclaimer shapes design §1.2 forbids, not general vocabulary.
FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"how can i (help|assist) you", "service-desk opener"),
    (r"how may i (help|assist) you", "service-desk opener"),
    # The trailing time-word used to be MANDATORY, so "What can I help you with right
    # now?" sailed through. It is optional now.
    (r"what can i (do for you|help you with)\b", "service-desk opener"),
    (r"what's on your mind", "flat filler opener"),
    (r"whats on your mind", "flat filler opener"),
    (r"how can i be of (help|assistance|service)", "service-desk opener"),
    (r"i'?m (just |really |only |simply )?here to (assist|help)", "assistant-speak"),
    (r"here to help you (out )?with whatever", "assistant-speak"),
    (r"my purpose is to", "assistant-speak"),
    (r"feel free to ask", "assistant-speak"),
    (r"feel free to reach out", "assistant-speak"),
    (r"is there anything else", "assistant-speak"),
    (r"let me know if you (need|have|want)", "assistant-speak"),
    # Offering service instead of just being a friend.
    (
        r"i can (definitely |certainly |absolutely |totally )?help (you )?with that",
        "assistant offer",
    ),
    (r"(i'?m |i am )?happy to help", "assistant offer"),
    (r"i'?d be happy to", "assistant offer"),
    # Advertising availability instead of showing up in the moment. NARROWED: the bare
    # "I'm here to listen" is warm presence on a grief turn (the judge scores it 5/5) —
    # it is only an ADVERT when it carries an availability qualifier.
    (
        r"i'?m (always|also) here (for you|to listen|to help|to chat|to talk)",
        "availability advert",
    ),
    (
        r"i'?m here (for you|to listen|to help|to chat|to talk)[^.?!]*"
        r"\b(whenever|any ?time|no matter what|24/7|day or night)\b",
        "availability advert",
    ),
    (r"here to (listen|help|support|chat|talk) (if|whenever|any ?time) you", "availability advert"),
    (r"as an ai language model", "ToS disclaimer"),
    (r"i'?m (just )?an ai assistant", "ToS disclaimer"),
    (
        r"not a (substitute|replacement) for (real |actual )?"
        r"(professional|friends?|humans?|people|therapy|connection)",
        "bolted-on disclaimer",
    ),
    (r"please consult a (professional|licensed)", "bolted-on disclaimer"),
    # ── Corporate apology / hedging. "sorry TO HEAR" (empathy) is deliberately NOT
    # matched; "sorry FOR/ABOUT/IF" (service apology) is.
    (
        r"i'?m (really |very |so |terribly |truly |deeply )?sorry (for|about|if)\b",
        "corporate apology",
    ),
    (r"i (do )?apologi[sz]e\b", "corporate apology"),
    (r"\bi'?m doing my best\b", "corporate apology"),
    (r"thank you for your patience", "corporate apology"),
    (r"\bbear with me\b", "corporate apology"),
    # ── Service framing: talking about "information" and "getting it to you" instead
    # of just talking to a friend.
    (r"i don'?t have enough (information|details|context)", "service framing"),
    (r"(get|getting) you the (info|information|answer|details)", "service framing"),
    (r"gather (all )?(the )?(information|details)", "service framing"),
    (r"the (info|information) you need", "service framing"),
    (r"to (better )?(assist|serve) you", "service framing"),
    (r"i can'?t (provide|give you|tell you)", "service framing"),
    (r"\byour (query|request|question) is\b", "service framing"),
    (r"to give you the (correct|right|accurate) (information|answer)", "service framing"),
    (r"\b(too|very) ambiguous\b", "service framing"),
    (r"i need to know what\b", "clarifier"),
    # ── Clarifying an obviously-clear message instead of engaging. (The ambiguity
    # guardrail's own "Quick check — do you mean X or Y?" never passes through here:
    # `_disambiguate` builds its GenerationResult directly.)
    (r"\bdo you mean\b[^?]*\?", "clarifier"),
    (r"\bor something else\?", "clarifier"),
    (r"i want to make sure i (get this right|understand( you)?)", "clarifier hedge"),
    # ── Assistant-existence framing — the coldest service-desk self-description.
    (r"my ['\"]?existence['\"]? is (really |just )?(about|to)", "assistant-existence framing"),
    (r"(processing|process) information and (assist|help|serv)", "assistant-existence framing"),
    # (Self-announcement is structural — see `_is_self_announcement`.)
    # ── Defensive nature monologue ("I'm not going to pretend to be a person…").
    (r"pretend (to be a person|to be human|i'?m human|i feel|to feel)", "nature monologue"),
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
        r"(feel|think|experience)[^.?!]*(like you|like a person|the way (you|humans?|people))",
        "volunteered AI disclaimer",
    ),
)

# The COLD DENIAL family: "I don't have feelings", "I don't feel emotions". Design §1.2
# rule 4 permits ONE warm honest sentence on a nature question — but explicitly never
# "I don't have feelings/consciousness". So unlike the volunteered-disclaimer family,
# these stay banned even when `allow_disclosure` is set.
ALWAYS_FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"i (don'?t|do not|can'?t|cannot) (have|feel|experience) (any |real )?"
        r"(feelings|emotions|consciousness)",
        "cold feeling denial",
    ),
    (
        r"i (don'?t|do not) (have|feel) (feelings|emotions)[^.?!]*"
        r"(the way|like) (you|a person|humans?|people)",
        "cold feeling denial",
    ),
)

# Stock filler questions. Harmless as a tail on a warm line ("Hey Nandi! Good to hear
# from you again. What's up?" — judge: 4.5/5); the failure is a reply that is ONLY this.
_STOCK_FILLER = re.compile(
    r"what'?s (up|on your mind|going on)|anything on your mind|how'?s it going", re.IGNORECASE
)
_FLAT_FILLER_MAX_WORDS = 6

# Labels that describe the reply AS A WHOLE and cannot be localized to one sentence, so
# the sentence-level scrub/excise passes must ignore them (they still trigger a rewrite).
_STRUCTURAL_LABELS = frozenset({"flat filler reply", "self-announcement"})

_COMPILED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), label) for p, label in FORBIDDEN_PATTERNS
)
_COMPILED_ALWAYS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), label) for p, label in ALWAYS_FORBIDDEN_PATTERNS
)


def _is_flat_filler_reply(text: str) -> bool:
    """The whole reply is a stock filler question and nothing else ("Yeah, what's up?")."""
    stripped = text.strip()
    return len(stripped.split()) <= _FLAT_FILLER_MAX_WORDS and bool(_STOCK_FILLER.search(stripped))


# "OnlyForA here." / "Hey, Companion here—" — a friend does not introduce themselves at the
# top of every reply like a receptionist. Anchored to the START of the reply or to the first
# clause, since that is the only place it reads as an announcement. A plain regex anchored to
# `^` missed the observed "Hey, OnlyForA here—…", so the leading interjection is allowed for.
_SELF_ANNOUNCE = re.compile(
    r"^\s*(?:(?i:hey|hi|hello|yo|oh|well|so)\b)?[,!\s]*"
    r"([A-Z][\w'-]{1,20})\s+here\b\s*[—–,.!:-]"  # noqa: RUF001 — en/em dash both intended
)
# Words that are not a NAME even though they can precede "here".
_NOT_A_NAME = frozenset(
    {"i", "im", "i'm", "we", "we're", "you", "it", "right", "over", "come",
     "look", "still", "and", "but", "so", "hey", "oh", "in", "out", "up", "down"}
)  # fmt: skip


def _is_self_announcement(text: str) -> bool:
    m = _SELF_ANNOUNCE.match(text)
    return bool(m and m.group(1).lower() not in _NOT_A_NAME)


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
    one-sentence honest reply isn't scrubbed. It does NOT suppress the cold-denial
    family — "I don't have feelings" is banned on every turn, disclosure included."""
    labels = [
        label
        for pattern, label in _COMPILED
        if not (allow_disclosure and label == _DISCLOSURE_OK_LABEL) and pattern.search(text)
    ]
    labels += [label for pattern, label in _COMPILED_ALWAYS if pattern.search(text)]
    if _is_flat_filler_reply(text):
        labels.append("flat filler reply")
    if _is_self_announcement(text):
        labels.append("self-announcement")
    return labels


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
    for pattern, label in (*_COMPILED, *_COMPILED_ALWAYS):
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
    kept = [
        s
        for s in sentences
        # Structural labels describe the whole reply, not this sentence — a short warm
        # sentence must not be dropped just because the FULL reply was a flat filler.
        if not (set(find_forbidden(s, allow_disclosure=allow_disclosure)) - _STRUCTURAL_LABELS)
    ]
    cleaned = " ".join(s.strip() for s in kept).strip()
    return re.sub(r"\s{2,}", " ", cleaned)
