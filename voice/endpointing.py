"""Semantic Endpointing (spec §21): has the user actually finished?

Combines silence duration with semantic completeness so a thinking pause is
never treated as end-of-turn. Complete thought → respond after the short
pause; incomplete (trailing conjunction, filler, mid-thought comma) → wait
the long pause. Thresholds come from the user's profile (§2) and are
learnable later (§18). The completeness check is a cheap lexical signal —
never the strong LLM (rule 5).
"""

import re

from pydantic import BaseModel

# Words that signal an unfinished thought when they end the partial.
_TRAILING_CONTINUATIONS = {
    "and", "or", "but", "so", "because", "then", "also", "plus", "like",
    "with", "about", "that", "which", "if", "when", "while", "to", "the",
    "a", "an", "my", "i",
}
_FILLERS = {"um", "uh", "er", "hmm", "uhh", "umm", "eh", "mmm"}

_WORD = re.compile(r"[a-zA-Z']+")


class EndpointDecision(BaseModel):
    respond: bool
    complete_thought: bool
    waited_ms: float
    threshold_ms: float


class SemanticEndpointer:
    def __init__(self, short_pause_ms: float = 700, long_pause_ms: float = 2500) -> None:
        self.short_pause_ms = short_pause_ms
        self.long_pause_ms = long_pause_ms

    def should_respond(
        self,
        partial_transcript: str,
        silence_ms: float,
        prosody_rising: bool | None = None,
    ) -> bool:
        return self.decide(partial_transcript, silence_ms, prosody_rising).respond

    def decide(
        self,
        partial_transcript: str,
        silence_ms: float,
        prosody_rising: bool | None = None,
    ) -> EndpointDecision:
        complete = is_complete_thought(partial_transcript)
        if prosody_rising:  # rising pitch = not done (rule 4, via §22)
            complete = False
        threshold = self.short_pause_ms if complete else self.long_pause_ms
        return EndpointDecision(
            respond=bool(partial_transcript.strip()) and silence_ms >= threshold,
            complete_thought=complete,
            waited_ms=silence_ms,
            threshold_ms=threshold,
        )


def is_complete_thought(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    words = [w.lower() for w in _WORD.findall(stripped)]
    if not words:
        return False
    last = words[-1]
    if last in _FILLERS:  # rule 3: never endpoint right after a filler
        return False
    if stripped.endswith(("...", "…", ",", "-", "—")):
        return False
    return last not in _TRAILING_CONTINUATIONS
