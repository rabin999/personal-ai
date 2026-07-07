"""Port: fast preference/personalization memory (Mem0 adapter, brief §2).

Complements the temporal knowledge graph (Graphiti, semantic facts that change)
and Qdrant episodic memory: this layer captures the user's standing *preferences*
and personal details with Mem0's own extraction + hybrid retrieval. Every call is
``user_id``-scoped (§0.5). Implementations must be best-effort — a failure here
never breaks a turn.
"""

from typing import Protocol


class PreferenceMemory(Protocol):
    async def add(self, user_id: str, messages: list[dict[str, str]]) -> None:
        """Let the preference engine extract + store anything memorable from this
        exchange (role/content messages). Best-effort; never raises into the turn."""
        ...

    async def search(self, user_id: str, query: str, limit: int = 5) -> list[str]:
        """Return this user's preferences/details relevant to ``query`` (strings)."""
        ...
