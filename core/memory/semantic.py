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

_SUBJECT_STARTS = ("the user", "user ", "user'", "they ", "he ", "she ")


def ensure_user_subject(fact: str) -> str:
    """Graphiti orphans a fact edge (and then can't retrieve it) when the episode
    has no explicit subject — verified: a bare "takes meds at 8pm" returns nothing,
    "The user takes…" returns reliably. So ensure every fact names the user."""
    stripped = fact.strip()
    if not stripped or stripped.lower().startswith(_SUBJECT_STARTS):
        return stripped
    return f"The user {stripped[0].lower()}{stripped[1:]}"


class SemanticMemory:
    def __init__(self, graph: GraphStore) -> None:
        self._graph = graph

    async def add_episode(self, user_id: str, text: str, timestamp: str | None = None) -> None:
        """Feed one episode; entities/relations/facts are extracted by the store."""
        await self._graph.add_episode(user_id, text, timestamp)

    async def record_fact(self, user_id: str, fact: str) -> None:
        """Store a distilled fact, ensuring it names the user so Graphiti attaches
        (and can retrieve) it. Correcting a fact = record the corrected version;
        Graphiti's temporal reasoning supersedes the contradicted one (§6 rule 2)."""
        await self._graph.add_episode(user_id, ensure_user_subject(fact))

    async def facts_for(self, user_id: str, entity_ids: list[str], limit: int = 10) -> list[Fact]:
        """Facts + relationships for resolved entities, with validity windows."""
        if not entity_ids:
            return []
        return await self._graph.search_facts(user_id, " ".join(entity_ids), limit=limit)

    async def profile_facts(self, user_id: str, limit: int = 10) -> list[Fact]:
        """Stable facts about the user themself."""
        return await self._graph.search_facts(user_id, _PROFILE_QUERY, limit=limit)

    async def all_facts(self, user_id: str, limit: int = 200) -> list[Fact]:
        """Every relationship fact (with connected entities) for the knowledge-graph
        view (U4) and cleanup (U1)."""
        return await self._graph.list_facts(user_id, limit=limit)

    async def delete_fact(self, user_id: str, uuid: str) -> bool:
        """Remove one accreted/gibberish fact edge (cleanup, U1), user-scoped."""
        return await self._graph.delete_fact(user_id, uuid)

    async def delete_all(self, user_id: str) -> int:
        """Wipe the user's ENTIRE knowledge graph (account deletion). Returns nodes removed."""
        return await self._graph.delete_all_for_user(user_id)
