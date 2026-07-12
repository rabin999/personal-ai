"""Response-style guardrail (design §1.2-1.4, §3; brief §7).

The companion is a warm friend, not a service desk. The tone standard lives in
config (`response_voice` trait) and is composed into the prompt — but nothing
*checked* that the model honored it. This module is the mechanical detector: it
flags the forbidden assistant-speak / ToS-disclaimer phrasings the design bans.

It is also the TRIGGER for self-reflection (§9.3): `_finalize` only runs the LLM
rewrite when this module flags something. So a detector that misses is a
self-reflection step that never fires.

**Calibrated against the LLM-judge OUT-OF-SAMPLE.** The bar is: *if the judge marks a reply
chatbot_like, the detector must flag it*, without flagging replies the judge passes. Both
halves matter — a miss ships a bad reply, a false alarm makes the reflection step rewrite a
reply that was already good.

`scripts/style_calibration.py` measures that agreement and **reports each source separately**,
because pooling them is what hid D-12: this module's phrases were harvested from
`docs/quality/baseline_live.json` and scored against `docs/quality/baseline_live.json`, where
they reported 1.000. On 104 replies they had never seen, they caught 0 of 22. Run the script
after touching this file, and read the HELD-OUT number.

Two mechanisms, in ascending order of generality:

- `FORBIDDEN_PATTERNS` — the original literal phrases. Kept: they are precise and free.
- `REGISTER_PATTERNS` — the *moves* a service desk makes (closing pleasantry, task
  acceptance, mechanism talk, unsolicited referral, info dump). These generalise.
- `LEAD_ONLY_PATTERNS` — matched against the opening sentence only, because the opening move
  is what sets the register.

Rules of thumb learned from calibration:
- "sorry **to hear** that" is warm empathy; "sorry **for/about/if**" is a service apology.
- "I'm here to listen" on a grief turn is warm; it only becomes an availability *advert*
  with a qualifier ("always", "whenever you need", "any time").
- A reply may apologise for a gap *after* it has answered; leading with the apology is the
  service desk. Hence the lead anchor.
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
    # Offering to go and do a task for the user, like a support agent taking a ticket.
    (r"i'?ll (check|look|get|find|pull) (that|it|this)( up)? for you", "assistant offer"),
    (r"let me (check|look|get|find|pull) (that|it|this)( up)? for you", "assistant offer"),
    (r"(want|happy) to help (you )?(sort|figure|work) (things? |this |it )?out", "assistant offer"),
    (r"help (you )?sort (things?|it|this) out", "assistant offer"),
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


# D-12. `FORBIDDEN_PATTERNS` above was harvested, phrase by phrase, from the 22 replies in
# `docs/quality/baseline_live.json` — and then tested against those same 22 replies, where it
# scored 1.000. On 104 replies it had never seen it caught ZERO of the 22 the judge flagged.
# A closed list of remembered strings cannot generalise; assistant-speak is an open set.
#
# These families are written from the REGISTER instead. Each names a *move* a service desk
# makes and a friend does not, so it survives phrasings the model has not used yet. Held-out
# recall is measured by `scripts/style_calibration.py` against replies they were NOT written
# from: 0.955 recall, 1.000 precision, 0 false alarms on the warm-reply controls.
#
# The one held-out miss is a news briefing delivered to someone in pain. That is a semantic
# failure with no lexical signature, and it is left to the judge rather than faked with a
# fragile regex.
REGISTER_PATTERNS: tuple[tuple[str, str], ...] = (
    # ── Closing pleasantry: winding up the turn by offering further service. The old
    # list knew "is there anything else" and therefore missed BOTH "Is there something
    # else I can help you with?" (the canonical shape) and "Is there anything I can do
    # to help?". Match the move: an offer-shaped question about what else we can do.
    (
        r"\bis there (?:anything|something|anything else|something else|"
        r"any(?:thing)? more)\b[^?]*\b(?:i|we) can\b[^?]*\?",
        "closing pleasantry",
    ),
    (r"\bis there (anything|something) else\b", "closing pleasantry"),
    (r"\banything else (i|we) can\b", "closing pleasantry"),
    # ── Task acceptance: a support agent taking a ticket. The old list enumerated the
    # verbs (check|look|get|find|pull), so "I'll GRAB that for you right away" — the
    # entire final reply to a price question (D-16) — went through untouched. Any verb.
    (
        r"\bi'?ll\s+(?:\w+\s+){0,2}(?:that|it|this|those|them)\b[^.?!]*\bfor you\b",
        "assistant offer",
    ),
    (r"\blet me\s+(?:just\s+)?\w+[^.?!]*\bfor you\b", "assistant offer"),
    (r"\bi'?ll do my best\b", "assistant offer"),
    (r"\bright away\b[^.?!]*[,!]?\s*$", "assistant offer"),
    # Restating the request back before acting on it — a ticket confirmation.
    (
        r"\bi understand (?:that )?you'?re (?:looking for|trying to|asking|after)\b",
        "service framing",
    ),
    # ── Mechanism talk: narrating the retrieval instead of answering. The user never
    # asked about "the search results"; a friend who looked something up just tells you.
    (r"\bthe search results?\b", "service framing"),
    (r"\bin the (?:info|information) i (?:just )?(?:looked up|found|have)\b", "service framing"),
    (r"\bit looks like i can (?:access|see|find|get)\b", "service framing"),
    # (Apologetic inability is LEAD-ANCHORED — see `_LEAD_ONLY_PATTERNS`.)
    # ── Hedged recommendation: "It looks like you might want an umbrella" is a weather
    # service reading a forecast aloud. A friend says "take an umbrella, it's pouring".
    (r"\bit looks like you (?:might|may|could) (?:want|need|like)\b", "hedged assistant framing"),
    # ── Unsolicited resource referral (design §6, §16). The companion is not a
    # helpline directory. Offering one to a bereaved person who asked for nothing is
    # the single worst thing in the gate run: 10/10 judged chatbot-like.
    (r"\bhelplines?\b", "unsolicited referral"),
    (r"\bsupport groups?\b", "unsolicited referral"),
    (r"\bhotlines?\b", "unsolicited referral"),
    (
        r"\bthere (?:are|'?s|is)\b[^.?!]*\b(?:resources?|places|people|organi[sz]ations?|options)\b"
        r"[^.?!]*\b(?:available|that can|who can|to help|offer|provide|support)\b",
        "unsolicited referral",
    ),
    (r"\bplease know that there (?:are|is)\b", "unsolicited referral"),
    (r"\bplease reach out\b", "unsolicited referral"),
    (r"\bplease be kind to yourself\b", "unsolicited referral"),
    # ── Info-dump framing: announcing a briefing instead of talking. Especially bad
    # on an emotional turn, where the design says meet the feeling first (§3.6.5).
    (r"\bi can tell you that there (?:are|is)\b", "info dump"),
    (r"\bit sounds like there'?s a lot going on\b[^.?!]*\bincluding\b", "info dump"),
    (
        r"\bhere(?:'?s| is) (?:a|the) (?:quick )?(?:summary|rundown|breakdown|list) of\b",
        "info dump",
    ),
)

# The COLD DENIAL family: "I don't have feelings", "I don't feel emotions". Design §1.2
# rule 4 permits ONE warm honest sentence on a nature question — but explicitly never
# "I don't have feelings/consciousness". So unlike the volunteered-disclaimer family,
# these stay banned even when `allow_disclosure` is set.
# Patterns matched ONLY against the reply's opening sentence, because the opening move is
# what sets the register. "I'm sorry, Nandi, I couldn't find the price" LEADS with a
# service-desk apology and carries no answer. "The index closed at 2601.92 … I'm sorry,
# though, I couldn't find the specific LTP for OP" answers first and apologises for a
# sub-part — which is what a friend does, and which the judge passed. Checking the whole
# reply flagged both; checking the lead separates them.
LEAD_ONLY_PATTERNS: tuple[tuple[str, str], ...] = (
    # "sorry TO HEAR" is warm empathy and is deliberately excluded by the lookahead.
    (
        r"i'?m (?:\w+ )?sorry\b(?! to hear)[^.?!]*\b"
        r"(?:couldn'?t|can'?t|still can'?t|cannot|unable to|wasn'?t able to)\s+"
        r"(?:find|locate|get|access|see|provide)\b",
        "apologetic inability",
    ),
    # Reading the request back before acting on it, the way a ticket is confirmed.
    # Lead-anchored because "you're looking for a reason to quit" is a fine thing to say
    # in the middle of a conversation about a job.
    (
        r"\b(?:i understand )?(?:that )?you'?re "
        r"(?:looking for|asking (?:me )?(?:for|about)|after|trying to find)\b",
        "request restatement",
    ),
)


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
    (re.compile(p, re.IGNORECASE), label) for p, label in (*FORBIDDEN_PATTERNS, *REGISTER_PATTERNS)
)
_COMPILED_ALWAYS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), label) for p, label in ALWAYS_FORBIDDEN_PATTERNS
)
_COMPILED_LEAD: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), label) for p, label in LEAD_ONLY_PATTERNS
)


def _lead(text: str) -> str:
    """The reply's opening sentence — the move that sets its register."""
    match = re.search(r"^[^.!?]*[.!?]", text.strip())
    return match.group(0) if match else text.strip()


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
    lead = _lead(text)
    labels += [label for pattern, label in _COMPILED_LEAD if pattern.search(lead)]
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


# ── the acknowledgement (D-8 / D-16) ─────────────────────────────────────────────

# A promise to fetch something. NOT the closed verb list `_HOLLOW_PROMISE` uses in
# response_gen.py (check|look|find|get|pull), which is why "I'll GRAB that for you right away"
# — the entire final reply to "what's the current LTP of OP?" — was not a promise to it.
#
# Two shapes, and the conjunction matters. A bare "I'll" is not a promise to act on the
# user's behalf: "I'll be honest, that one stung" is a friend talking. What makes it a
# service ticket is the beneficiary ("… for you") or a stalling idiom.
_PROMISE_FOR_YOU = re.compile(
    r"\b(?:i'?ll|i will|i'?m going to|i'?m about to|let me)\b[^.?!]*\bfor you\b",
    re.IGNORECASE,
)
_PROMISE_IDIOM = re.compile(
    r"\b(?:hang on|hold on|give me a (?:moment|sec|second)|one (?:moment|sec|second)|"
    r"just a (?:moment|sec|second)|(?:i'?m )?on it|right away|coming (?:right )?up|"
    r"bear with me)\b",
    re.IGNORECASE,
)
# "I'll check that", "let me pull it up", "I'll grab this" — a verb whose object is the
# thing the user asked about. Any verb: enumerating them is what let "grab" through.
# "I'll be honest, that one stung" does not match: the object does not follow the verb.
_PROMISE_OBJECT = re.compile(
    r"\b(?:i'?ll|i will|let me)\s+(?:just\s+|quickly\s+|go\s+)?\w+\s+(?:that|it|this|those|them)\b",
    re.IGNORECASE,
)


def _is_promise(sentence: str) -> bool:
    return bool(
        _PROMISE_FOR_YOU.search(sentence)
        or _PROMISE_IDIOM.search(sentence)
        or _PROMISE_OBJECT.search(sentence)
    )


# Words that carry no answer. A sentence made only of these is filler, however warm.
_FILLER_WORDS = frozenset(
    {"oh", "ah", "hey", "hi", "hello", "yeah", "yes", "okay", "ok", "sure", "right", "well",
     "so", "now", "then", "there", "here", "you", "your", "i", "me", "my", "it", "that",
     "this", "the", "a", "an", "and", "but", "for", "to", "of", "is", "are", "am", "was",
     "nandi", "again", "please", "thanks", "thank"}
)  # fmt: skip

# Below this many content words (and with no digit anywhere), a reply carries no answer.
_MIN_ANSWER_WORDS = 3


def _carries_an_answer(sentence: str) -> bool:
    """Does this sentence say anything the user could not have written themselves?"""
    if any(ch.isdigit() for ch in sentence):
        return True
    words = [w.strip(",.;:!?'\"—-").lower() for w in sentence.split()]
    return len([w for w in words if w and w not in _FILLER_WORDS]) >= _MIN_ANSWER_WORDS


def is_bare_acknowledgement(text: str, *, allow_disclosure: bool = False) -> bool:
    """True when the reply is a PROMISE TO ACT carrying no answer (D-8, D-16).

    "I'll grab that for you right away, Nandi!" was the entire final spoken reply to "what's
    the current LTP of OP?". The user asked for a price and was told one would be fetched.

    This is structural, not lexical: drop every sentence that is a promise, a restatement of
    the request, or otherwise flagged assistant-speak, and ask whether anything answering
    remains. It therefore also catches the shape from SESSION_REPORT_GATE_RERUN §3.2(b) —
    "Oh, you're looking for the current LTP for OP again. I'll check that for you right now."
    — where the first sentence merely reads the question back.

    An empty reply is not an acknowledgement; it is D-9, and the caller handles it.
    """
    if not text.strip():
        return False
    sentences = _SENTENCE.findall(text)
    substantive = [
        s
        for s in sentences
        if not _is_promise(s)
        and not find_forbidden(s, allow_disclosure=allow_disclosure)
        and _carries_an_answer(s)
    ]
    if substantive:
        return False
    # Nothing answering survived. It is only an ACK if a promise was actually made —
    # otherwise this is just a short warm line ("Oh Nandi, I'm so sorry."), which is fine.
    return _is_promise(text)


# Frat-boy slang vocatives/interjections. Removed SURGICALLY (not by dropping the whole
# sentence like scrub_forbidden) so the real content survives: "Oh dude, that's great" →
# "That's great". The instruction forbids these for a students/professionals audience
# (design §12 response standard); this is the deterministic backstop. "man" is deliberately
# excluded — too many legitimate uses ("the man", "man-made") to strip safely.
_SLANG_LEAD = re.compile(
    r"^\s*(?:oh|ah|hey|yo|ayy?)?[\s,]*\b(?:dude|bro|bruh|homie|fam|mate)\b[\s,!.–—-]*",  # noqa: RUF001 — en/em dash both intended
    re.IGNORECASE,
)
_SLANG_VOCATIVE = re.compile(
    r"\s*,\s*\b(?:dude|bro|bruh|homie|fam)\b(?=[\s,.!?]|$)",
    re.IGNORECASE,
)


def strip_slang(text: str) -> str:
    """Surgically remove slang vocatives ('dude', 'bro', …), preserving the real
    content and re-capitalizing the new opening. Backstop to the no-slang instruction."""
    out = _SLANG_VOCATIVE.sub("", _SLANG_LEAD.sub("", text))
    out = re.sub(r"\s{2,}", " ", out).strip()
    if out and out[0].islower() and text[:1].isupper():
        out = out[0].upper() + out[1:]
    return out or text  # never empty the reply to nothing


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
