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
    (r"i'?m here to (assist|help)", "assistant-speak"),
    (r"my purpose is to", "assistant-speak"),
    (r"feel free to ask", "assistant-speak"),
    (r"is there anything else i can help", "assistant-speak"),
    (r"as an ai language model", "ToS disclaimer"),
    (r"i'?m (just )?an ai assistant", "ToS disclaimer"),
    (r"not a substitute for (real )?(professional|friends|human)", "bolted-on disclaimer"),
    (r"please consult a (professional|licensed)", "bolted-on disclaimer"),
)

_COMPILED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), label) for p, label in FORBIDDEN_PATTERNS
)


def find_forbidden(text: str) -> list[str]:
    """Return labels for every forbidden phrasing found in ``text`` (empty = clean)."""
    return [label for pattern, label in _COMPILED if pattern.search(text)]


def is_assistant_speak(text: str) -> bool:
    return bool(find_forbidden(text))
