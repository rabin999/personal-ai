"""Dynamic User Persona (design §2 Social Intelligence/Adaptability, §6; brief U0-U2).

The persona is the "How I've learned to talk with you" layer — DISTINCT from facts
(what is true about the user → semantic memory) and events (what happened →
episodic). The persona is DELIVERY/STYLE: how THIS user likes to be talked to and
who they are as a conversational partner (short vs. detailed, humor tolerance,
formality, warmth, pace; topic interests; emotionally sensitive topics).

Three properties the brief requires:
1. **Dynamic & per-user** — a maintained record, not static fixed data.
2. **Evolves** — repeated signals raise confidence; a *directly stated* preference
   ("keep it short") lands high-confidence immediately and shapes the very next
   reply; a merely-inferred style accrues slowly. A contradicting signal on the
   same dimension SUPERSEDES the old one via a validity window (never freeze a
   first impression, don't keep stale + new).
3. **Drives responses** — ``render_for_prompt`` injects the active persona into
   Prompt Assembly so the SAME question gets a different STYLE per user.

Persona notes are refined by the background memory-routing worker (reads unrouted
turns via the cursor; no double-write) — the same worker that routes facts/events,
so this is not a new pipeline. Every write is ``user_id``-scoped (§0.5).
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ports.doc_store import DocStore

logger = logging.getLogger(__name__)

PERSONA_COLLECTION = "persona"

# Style dimensions the persona tracks. A note tagged with one of these can be
# SUPERSEDED by a later contradicting signal on the same dimension (validity
# window); interests/sensitivities have no dimension and are deduped by text.
STYLE_DIMENSIONS = (
    "brevity",  # short/blunt ↔ detailed/thorough
    "directness",  # get-to-the-point ↔ soft/cushioned
    "warmth",  # wants warmth/affection ↔ matter-of-fact
    "humor",  # enjoys humor/banter ↔ keep it straight
    "formality",  # casual ↔ formal
    "pace",  # slow/patient ↔ fast/snappy
)
Kind = Literal["style", "interest", "sensitivity"]

# A directly-STATED preference ("keep it short") is trusted immediately — it shapes
# the very next reply. A merely-INFERRED style starts low and must be reinforced by
# repetition before it's injected (distinguishes a durable shift from a one-off mood).
STATED_CONFIDENCE = 0.65
INFERRED_CONFIDENCE = 0.3
CONFIDENCE_GAIN = 0.15
# Above this, a note is confident enough to shape responses (injected into the prompt).
INJECT_THRESHOLD = 0.5


class PersonaNote(BaseModel):
    """One learned, readable style/interest signal about how to talk with the user."""

    id: str
    text: str  # 2nd-person, readable: "You like me to get to the point"
    kind: Kind = "style"
    dimension: str | None = None  # one of STYLE_DIMENSIONS for style notes
    confidence: float = Field(default=INFERRED_CONFIDENCE, ge=0.0, le=1.0)
    evidence_count: int = 1
    valid_from: str
    valid_to: str | None = None  # set when superseded by a contradicting signal


class Persona(BaseModel):
    user_id: str
    notes: list[PersonaNote] = Field(default_factory=list)
    updated_at: str = ""

    def active(self) -> list[PersonaNote]:
        return [n for n in self.notes if n.valid_to is None]


class StyleSignal(BaseModel):
    """A persona signal extracted from a turn (from the LLM memory step)."""

    text: str
    kind: Kind = "style"
    dimension: str | None = None
    stated: bool = False  # True = user directly stated the preference this turn


class PersonaStore:
    def __init__(self, docs: DocStore, inject_threshold: float = INJECT_THRESHOLD) -> None:
        self._docs = docs
        self._threshold = inject_threshold

    async def get(self, user_id: str) -> Persona:
        doc = await self._docs.get(PERSONA_COLLECTION, user_id)
        if doc is None:
            return Persona(user_id=user_id)
        return Persona.model_validate(
            {"user_id": user_id, **{k: v for k, v in doc.items() if k != "_id"}}
        )

    async def apply(self, user_id: str, signals: list[StyleSignal]) -> int:
        """Merge new style signals into the persona: reinforce a repeat, supersede a
        contradiction on the same dimension (validity window), or add a new note.
        Returns how many notes were changed. Deterministic (unit-testable); the LLM
        only supplies the signals."""
        if not signals:
            return 0
        persona = await self.get(user_id)
        changed = 0
        now = datetime.now(UTC).isoformat()
        for signal in signals:
            text = signal.text.strip()
            if not text:
                continue
            dim = signal.dimension if signal.dimension in STYLE_DIMENSIONS else None
            match = _find_match(persona, text, dim)
            if match is None:
                persona.notes.append(
                    PersonaNote(
                        id=str(uuid.uuid4()),
                        text=text,
                        kind=signal.kind,
                        dimension=dim,
                        confidence=STATED_CONFIDENCE if signal.stated else INFERRED_CONFIDENCE,
                        evidence_count=1,
                        valid_from=now,
                    )
                )
                changed += 1
            elif _same_direction(match.text, text):
                # Reinforce: raise confidence, refresh wording, keep the note.
                match.confidence = min(1.0, match.confidence + CONFIDENCE_GAIN)
                match.evidence_count += 1
                match.text = text
                changed += 1
            else:
                # Contradiction on the same dimension → supersede the old, add the new.
                match.valid_to = now
                persona.notes.append(
                    PersonaNote(
                        id=str(uuid.uuid4()),
                        text=text,
                        kind=signal.kind,
                        dimension=dim,
                        confidence=STATED_CONFIDENCE if signal.stated else INFERRED_CONFIDENCE,
                        evidence_count=1,
                        valid_from=now,
                    )
                )
                changed += 1
        if changed:
            await self._save(persona)
        return changed

    async def render_for_prompt(self, user_id: str) -> str:
        """The HOW-to-talk block injected into Prompt Assembly (§10) — only notes
        confident enough to act on. Empty until something is learned."""
        persona = await self.get(user_id)
        confident = [n for n in persona.active() if n.confidence >= self._threshold]
        if not confident:
            return ""
        confident.sort(key=lambda n: n.confidence, reverse=True)
        style = [n.text for n in confident if n.kind == "style"]
        interests = [n.text for n in confident if n.kind == "interest"]
        sensitivities = [n.text for n in confident if n.kind == "sensitivity"]
        lines: list[str] = []
        for text in [*style, *sensitivities, *interests]:
            lines.append(f"- {text}")
        return (
            "How THIS person likes you to talk with them (learned over time — let it "
            "genuinely shape your tone, length, and style this turn):\n" + "\n".join(lines)
        )

    async def readable(self, user_id: str) -> list[dict[str, Any]]:
        """Active persona notes for the Memories UI ('How I've learned to talk with you')."""
        persona = await self.get(user_id)
        notes = sorted(persona.active(), key=lambda n: n.confidence, reverse=True)
        return [
            {
                "id": n.id,
                "text": n.text,
                "kind": n.kind,
                "dimension": n.dimension,
                "confidence": n.confidence,
                "evidence_count": n.evidence_count,
                "active": n.confidence >= self._threshold,
            }
            for n in notes
        ]

    async def _save(self, persona: Persona) -> None:
        persona.updated_at = datetime.now(UTC).isoformat()
        doc = persona.model_dump()
        doc["_id"] = doc.pop("user_id")
        await self._docs.put(PERSONA_COLLECTION, persona.user_id, doc)


def _find_match(persona: Persona, text: str, dim: str | None) -> PersonaNote | None:
    """An active note this signal refers to: the same style dimension, or (for
    interests/sensitivities with no dimension) a text-similar note."""
    if dim is not None:
        for note in persona.active():
            if note.dimension == dim:
                return note
        return None
    lowered = _words(text)
    for note in persona.active():
        if note.dimension is None and _overlap(lowered, _words(note.text)) >= 0.6:
            return note
    return None


# Words that flip a style statement's polarity — used to tell "reinforce" (same
# direction) from "supersede" (contradiction) on a shared dimension.
_NEGATORS = frozenset(
    {
        "short",
        "long",
        "brief",
        "detailed",
        "concise",
        "thorough",
        "blunt",
        "gentle",
        "direct",
        "soft",
        "formal",
        "casual",
        "warm",
        "cool",
        "fast",
        "slow",
        "more",
        "less",
        "don't",
        "dont",
        "not",
        "no",
        "stop",
    }
)
_ANTONYMS = (
    frozenset({"short", "brief", "concise"}),
    frozenset({"long", "detailed", "thorough"}),
    frozenset({"blunt", "direct"}),
    frozenset({"gentle", "soft", "cushioned"}),
    frozenset({"formal"}),
    frozenset({"casual", "relaxed"}),
    frozenset({"fast", "snappy", "quick"}),
    frozenset({"slow", "patient", "unhurried"}),
)


def _same_direction(existing: str, incoming: str) -> bool:
    """True when two same-dimension statements point the SAME way (reinforce), False
    when they contradict (supersede). Deterministic polarity check over antonym sets."""
    a, b = _words(existing) & _NEGATORS, _words(incoming) & _NEGATORS
    if not a or not b:
        return True  # no clear polarity marker → treat as reinforcement
    for left in _ANTONYMS:
        for right in _ANTONYMS:
            if left == right:
                continue
            if (a & left) and (b & right):
                return False
    return True


def _words(text: str) -> set[str]:
    return {w.strip(".,!?'\"").lower() for w in text.split() if len(w) > 2}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))
