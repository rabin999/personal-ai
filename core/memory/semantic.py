"""Semantic Memory (spec §6): durable facts and relationships with temporal validity.

Answers "who is X" and "what changed". Extraction happens when episodes are
added — by Consolidation (§18), never in the live conversation path (rule 1).
Superseded facts keep their history: ``valid_to`` marks them, nothing is
deleted (rule 2); retrieval returns validity windows (rule 3).
"""

from ports.graph_store import Fact, GraphStore

# Broad standing query for stable facts about the user themselves; entity-
# specific lookups go through facts_for.
_PROFILE_QUERY = (
    "facts about the user: identity, family, friends, relationships, "
    "preferences, work, projects, health, plans"
)


class SemanticMemory:
    def __init__(self, graph: GraphStore) -> None:
        self._graph = graph

    async def add_episode(self, user_id: str, text: str, timestamp: str | None = None) -> None:
        """Feed one episode; entities/relations/facts are extracted by the store."""
        await self._graph.add_episode(user_id, text, timestamp)

    async def facts_for(self, user_id: str, entity_ids: list[str], limit: int = 10) -> list[Fact]:
        """Facts + relationships for resolved entities, with validity windows."""
        if not entity_ids:
            return []
        return await self._graph.search_facts(user_id, " ".join(entity_ids), limit=limit)

    async def profile_facts(self, user_id: str, limit: int = 10) -> list[Fact]:
        """Stable facts about the user themself."""
        return await self._graph.search_facts(user_id, _PROFILE_QUERY, limit=limit)
