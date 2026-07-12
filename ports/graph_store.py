"""Port: graph store (Graphiti + Neo4j adapter) — semantic memory with temporal validity (spec §6).

Facts carry validity windows: ``valid_to`` set means superseded (never
deleted); ``valid_to`` None means currently true.
"""

from typing import Protocol

from pydantic import BaseModel


class Fact(BaseModel):
    fact: str
    relation: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    created_at: str | None = None
    uuid: str | None = None  # graph edge id — enables targeted delete (cleanup, U1)
    source: str | None = None  # entity the relationship starts at (graph view, U4)
    target: str | None = None  # entity the relationship points to (graph view, U4)

    @property
    def is_current(self) -> bool:
        return self.valid_to is None


class GraphStore(Protocol):
    async def setup(self) -> None:
        """Build graph indices/constraints; idempotent, called at startup."""
        ...

    async def add_episode(self, user_id: str, text: str, timestamp: str | None = None) -> None:
        """Feed one episode; the adapter extracts entities/relations/facts."""
        ...

    async def search_facts(self, user_id: str, query: str, limit: int = 10) -> list[Fact]:
        """Relevant facts for this user only, each with its validity window."""
        ...

    async def list_facts(self, user_id: str, limit: int = 200) -> list[Fact]:
        """All of this user's relationship facts (source/target/validity/uuid) for the
        knowledge-graph view (U4) and cleanup (U1). User-scoped by construction."""
        ...

    async def delete_fact(self, user_id: str, uuid: str) -> bool:
        """Hard-delete one relationship edge (cleanup of accreted junk, U1). Scoped to
        the user's group so it can never touch another user's graph. Returns removed."""
        ...

    async def delete_all_for_user(self, user_id: str) -> int:
        """Wipe the WHOLE knowledge graph for one user (account deletion). Group-scoped so
        it can never touch another user's graph (§0.5). Returns nodes deleted."""
        ...
