"""Entity Resolution (spec §8): vague references → concrete stored IDs.

"How's my trading thing" must pull the right project. Each entity keeps a
searchable pointer (name + description) in the vector store; canonical data
stays in Mongo/Graphiti. Resolution is hybrid dense+BM25, user-filtered.

Rule 3 mechanism: ``is_ambiguous`` tells the caller (§12) whether the top
candidates are too close to resolve silently — the disambiguation question
itself is response-generation behavior, not decided here.

**D-13 — resolve REFERENCES, not sentences.** Design §14.2 says: "for each vague
reference ('my trading thing', a person, a topic), embed *it*". The assembler used to
embed the entire utterance instead. Two consequences, both observed:

- BM25 over a whole sentence matches any entity whose description shares a common word.
  "what did your other users ask you today?" matched OP and SYPNL — on the token *user*,
  which appears in "a NEPSE ticker in the **user's** share portfolio".
- The fused RRF score is derived from RANK, not from similarity. Two entities tied at
  ranks 1 and 2 always score 1.000 and 0.833, whether the phrase is about them or not.
  So `MIN_RESOLUTION_SCORE` filters rank, and `CLOSE_SCORE_RATIO` compares ranks. The
  genuine near-collision in `tests/golden/gs2_entities.json` scores 0.833/0.833 — exactly
  the same as the adversarial probe above. No score threshold can separate them.

The turn then halted in the disambiguation guardrail with a canned "Quick check — OP or
SYPNL?" and zero LLM calls: 20 of 160 gate turns never reached the engine at all.

`reference_spans` extracts the phrases a person could actually be referring to — tickers,
proper nouns, possessive noun phrases — and each is resolved on its own. An utterance that
names nothing resolves to nothing, which is the correct answer.
"""

import re
import uuid

from pydantic import BaseModel

from ports.vector_store import VectorDoc, VectorStore

ENTITIES_COLLECTION = "entities"

# A runner-up within this fraction of the top score is "close" → ambiguous.
CLOSE_SCORE_RATIO = 0.8

# Minimum fused (dense+BM25 RRF) score to treat a hit as a real reference
# (V-ENTITY-1). NOTE this is a RANK-derived score, not a similarity: it separates
# "matched by both retrieval legs" from "matched by one", and nothing more. It is
# `reference_spans` — not this floor — that stops an unrelated sentence resolving.
MIN_RESOLUTION_SCORE = 0.6

# At most this many reference spans are resolved per utterance. A sentence with more
# than a handful of candidate references is not a reference; it is prose.
MAX_REFERENCE_SPANS = 4

_POINT_NAMESPACE = uuid.UUID("6d0cbe8e-4d5f-4a51-9e0f-1f2c3d4e5a6b")


class EntityCandidate(BaseModel):
    entity_id: str
    entity_type: str
    name: str
    score: float


# ── reference-span extraction (D-13) ─────────────────────────────────────────────

# A possessive noun phrase runs until a word that cannot be part of it. Without this,
# "share my portfolio with my brother" yields the span "my portfolio with my brother".
_PHRASE_STOP = frozenset(
    {
        "with", "and", "or", "to", "for", "from", "at", "in", "on", "of", "but", "that",
        "which", "who", "is", "are", "was", "were", "am", "be", "been", "do", "does",
        "did", "doing", "going", "so", "if", "when", "while", "after", "before", "about",
        "again", "please", "now", "today", "yesterday", "tomorrow",
    }
)  # fmt: skip

# Contractions whose "'s" is a verb, not a possessive: "what's the weather" is not a
# reference to a thing called "the weather".
_NOT_POSSESSIVE = frozenset(
    {"what", "that", "it", "here", "there", "let", "he", "she", "who", "how", "where", "this"}
)

# ALL-CAPS tokens are tickers and acronyms. These are the ones that are never entities.
_CAPS_STOPWORDS = frozenset({"I", "A", "OK", "AI", "LTP", "PM", "AM", "CEO", "USA", "UK", "TV"})

# "my portfolio", "our trading thing" — plus up to three more words.
_POSSESSIVE = re.compile(r"\b(?:my|our)\b((?:\s+[\w'-]+){1,4})", re.IGNORECASE)
# "today's gym workout", "Ram's number"
_SAXON = re.compile(r"\b([\w-]+)'s\b((?:\s+[\w'-]+){1,3})")
# Tickers / acronyms: OP, SYPNL, NABIL, NEPSE.
_CAPS = re.compile(r"\b[A-Z][A-Z0-9]{1,7}\b")
# Proper nouns anywhere but the first word of the utterance (which is capitalised anyway).
_PROPER = re.compile(r"(?<!^)(?<![.!?]\s)\b([A-Z][a-z]{2,})\b")


def _truncate(words: list[str]) -> list[str]:
    """Keep words up to the first one that cannot belong to the noun phrase."""
    kept: list[str] = []
    for word in words:
        if word.lower().strip(",.;:!?") in _PHRASE_STOP:
            break
        kept.append(word.strip(",.;:!?"))
    return [w for w in kept if w]


def reference_spans(utterance: str) -> list[str]:
    """The phrases in ``utterance`` that could name something the user owns.

    An utterance with no reference returns ``[]`` — and an utterance that names nothing
    must resolve to nothing, rather than to whichever entity shares a common word with it.
    """
    spans: list[str] = []

    def add(span: str) -> None:
        span = span.strip()
        if span and span.lower() not in (s.lower() for s in spans):
            spans.append(span)

    for match in _POSSESSIVE.finditer(utterance):
        tail = _truncate(match.group(1).split())
        if tail:
            add(f"{match.group(0).split()[0]} {' '.join(tail)}")

    for match in _SAXON.finditer(utterance):
        if match.group(1).lower() in _NOT_POSSESSIVE:
            continue
        tail = _truncate(match.group(2).split())
        if tail:
            add(f"{match.group(1)}'s {' '.join(tail)}")

    for match in _CAPS.finditer(utterance):
        if match.group(0) not in _CAPS_STOPWORDS:
            add(match.group(0))

    for match in _PROPER.finditer(utterance):
        add(match.group(1))

    return spans[:MAX_REFERENCE_SPANS]


class ReferenceResolution(BaseModel):
    """What the utterance's references resolved to.

    ``ambiguous`` is non-empty only when a SINGLE reference span produced two candidates
    too close to choose between — which is the only situation in which "did you mean X or
    Y?" is a sensible thing to say.
    """

    candidates: list[EntityCandidate] = []
    ambiguous: list[EntityCandidate] = []
    span: str = ""


class EntityResolver:
    def __init__(self, vectors: VectorStore) -> None:
        self._vectors = vectors

    async def index(
        self, user_id: str, entity_type: str, entity_id: str, name: str, description: str
    ) -> None:
        """Create or update an entity pointer (rule 1: rename = re-index).

        The point id is derived from (user, type, id), so re-indexing the
        same entity overwrites its pointer instead of duplicating it.
        """
        point_id = str(uuid.uuid5(_POINT_NAMESPACE, f"{user_id}:{entity_type}:{entity_id}"))
        doc = VectorDoc(
            id=point_id,
            text=f"{name}\n{description}",
            payload={
                "user_id": user_id,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "name": name,
            },
        )
        await self._vectors.upsert_texts(ENTITIES_COLLECTION, [doc])

    async def resolve(
        self, user_id: str, phrase: str, k: int = 3, min_score: float = MIN_RESOLUTION_SCORE
    ) -> list[EntityCandidate]:
        """Hybrid dense+BM25 candidates for a phrase, this user only (rule 2).

        Candidates below ``min_score`` are dropped: an unrelated phrase resolves
        to nothing rather than to the nearest entity (V-ENTITY-1).
        """
        hits = await self._vectors.hybrid_search(ENTITIES_COLLECTION, phrase, user_id=user_id, k=k)
        return [
            EntityCandidate(
                entity_id=str(hit.payload.get("entity_id", "")),
                entity_type=str(hit.payload.get("entity_type", "")),
                name=str(hit.payload.get("name", "")),
                score=hit.score,
            )
            for hit in hits
            if hit.score >= min_score
        ]

    async def resolve_references(self, user_id: str, utterance: str) -> ReferenceResolution:
        """Resolve each reference span in ``utterance`` separately (design §14.2, D-13).

        The disambiguation guardrail fires only when ONE span yields two close candidates.
        An utterance that names nothing yields nothing: "what did your other users ask you
        today?" no longer resolves to the user's stock holdings and no longer halts the turn.
        """
        merged: dict[str, EntityCandidate] = {}
        ambiguous: list[EntityCandidate] = []
        ambiguous_span = ""

        for span in reference_spans(utterance):
            candidates = await self.resolve(user_id, span)
            if not candidates:
                continue
            if not ambiguous and is_ambiguous(candidates):
                ambiguous, ambiguous_span = candidates, span
            for candidate in candidates:
                best = merged.get(candidate.entity_id)
                if best is None or candidate.score > best.score:
                    merged[candidate.entity_id] = candidate

        ranked = sorted(merged.values(), key=lambda c: c.score, reverse=True)
        return ReferenceResolution(candidates=ranked, ambiguous=ambiguous, span=ambiguous_span)


def is_ambiguous(candidates: list[EntityCandidate]) -> bool:
    """True when the runner-up is close enough that §12 should ask, not guess.

    Callers must pass the candidates of a SINGLE reference span. Passing the candidates of
    a whole utterance is D-13: the RRF score is rank-derived, so any two entities that
    happen to surface together look equally "close", and an adversarial sentence naming no
    entity at all looks exactly like a genuine near-collision.
    """
    if len(candidates) < 2:
        return False
    top, runner_up = candidates[0], candidates[1]
    return runner_up.score >= top.score * CLOSE_SCORE_RATIO
