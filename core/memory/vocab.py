"""STT vocabulary source — the user's own names/terms (spec §20 rule 2, §10.3).

Seeds the STT decoder with words that generic speech models mangle: the
companion's name, people, places, project terms (e.g. "Trishul", "NEPSE"),
pulled from Semantic Memory (§6) and the profile. Passed as ``vocab`` to
``stt.transcribe_stream`` so a user-specific term is transcribed correctly
instead of collapsing into a common word.

Best-effort and read-only: a store hiccup yields an empty list, never an error
on the latency-critical voice path.
"""

import re

from core.memory.semantic import SemanticMemory
from core.profile import ProfileService

# Proper-noun-ish tokens in a fact sentence ("The user works on NEPSE with Ram").
_PROPER_NOUN = re.compile(r"\b([A-Z][A-Za-z0-9][A-Za-z0-9'-]{1,})\b")
# Sentence-initial / generic capitalised words that aren't user vocabulary.
_GENERIC = frozenset(
    {
        "The",
        "This",
        "That",
        "They",
        "Their",
        "There",
        "Then",
        "These",
        "Those",
        "User",
        "Companion",
        "When",
        "What",
        "Where",
        "Which",
        "While",
        "With",
        "And",
        "But",
        "For",
        "Her",
        "His",
        "She",
        "Him",
        "Named",
        "Call",
        "Calls",
    }
)
_MAX_TERMS = 50  # keep the STT prompt short; most-relevant facts first


class VocabProvider:
    def __init__(self, semantic: SemanticMemory, profiles: ProfileService) -> None:
        self._semantic = semantic
        self._profiles = profiles

    async def terms_for(self, user_id: str) -> list[str]:
        terms: list[str] = []
        seen: set[str] = set()

        def _add(term: str) -> None:
            key = term.lower()
            if term and key not in seen:
                seen.add(key)
                terms.append(term)

        try:
            profile = await self._profiles.get(user_id)
            if profile.companion_name:
                _add(profile.companion_name)
        except Exception:  # profile read is best-effort on the voice path
            pass

        try:
            facts = await self._semantic.profile_facts(user_id, limit=20)
        except Exception:
            facts = []
        for fact in facts:
            for token in _PROPER_NOUN.findall(fact.fact):
                if token not in _GENERIC:
                    _add(token)
            if len(terms) >= _MAX_TERMS:
                break

        return terms[:_MAX_TERMS]
