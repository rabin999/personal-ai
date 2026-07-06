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
