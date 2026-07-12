"""Adapter: Graphiti + Neo4j graph store (implements ports.graph_store.GraphStore, spec §6).

Multi-tenancy via Graphiti group_ids: every episode is written with
``group_id = user_id`` and every search is filtered to that group — one
user's graph is invisible to another's queries.

Graphiti drives its own LLM calls (outside the §11 router), so after each
operation this adapter drains the shared usage recorder into the Cost
Ledger, priced from the ``llm_pricing`` provider config.
"""

from collections.abc import Mapping
from datetime import UTC, datetime

from adapters.db import Database
from core.cost import CostEntry, CostLedger, CostMetadata
from ports.graph_store import Fact


class GraphitiGraphStore:
    def __init__(
        self,
        db: Database,
        ledger: CostLedger | None = None,
        pricing: Mapping[str, Mapping[str, float]] | None = None,
    ) -> None:
        self._db = db
        self._ledger = ledger
        self._pricing = dict(pricing or {})

    async def setup(self) -> None:
        await self._db.graphiti().build_indices_and_constraints()

    async def add_episode(self, user_id: str, text: str, timestamp: str | None = None) -> None:
        reference_time = datetime.fromisoformat(timestamp) if timestamp else datetime.now(UTC)
        try:
            await self._db.graphiti().add_episode(
                name=f"episode-{reference_time.isoformat()}",
                episode_body=text,
                source_description="conversation transcript",
                reference_time=reference_time,
                group_id=user_id,
            )
        finally:
            self._log_llm_usage(user_id, task="semantic.add_episode")

    async def search_facts(self, user_id: str, query: str, limit: int = 10) -> list[Fact]:
        try:
            edges = await self._db.graphiti().search(
                query=query, group_ids=[user_id], num_results=limit
            )
        finally:
            self._log_llm_usage(user_id, task="semantic.search")
        return [
            Fact(
                fact=edge.fact,
                relation=edge.name,
                valid_from=_iso(getattr(edge, "valid_at", None)),
                valid_to=_iso(getattr(edge, "invalid_at", None)),
                created_at=_iso(getattr(edge, "created_at", None)),
                uuid=getattr(edge, "uuid", None),
            )
            for edge in edges
        ]

    async def list_facts(self, user_id: str, limit: int = 200) -> list[Fact]:
        """Every relationship edge in this user's graph (group_id-scoped), with the
        connected entity names — powers the knowledge-graph view (U4) and cleanup (U1).
        Read directly over Neo4j because Graphiti's search is query-driven, not a full
        enumeration."""
        cypher = (
            "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) "
            "WHERE r.group_id = $gid AND r.fact IS NOT NULL "
            "RETURN a.name AS source, b.name AS target, r.fact AS fact, r.name AS relation, "
            "r.uuid AS uuid, r.valid_at AS valid_at, r.invalid_at AS invalid_at, "
            "r.created_at AS created_at "
            "ORDER BY r.created_at DESC LIMIT $limit"
        )
        async with self._db.neo4j().session() as session:
            result = await session.run(cypher, gid=user_id, limit=limit)
            records = [record.data() async for record in result]
        return [
            Fact(
                fact=rec.get("fact", ""),
                relation=rec.get("relation"),
                source=rec.get("source"),
                target=rec.get("target"),
                uuid=rec.get("uuid"),
                valid_from=_neo_iso(rec.get("valid_at")),
                valid_to=_neo_iso(rec.get("invalid_at")),
                created_at=_neo_iso(rec.get("created_at")),
            )
            for rec in records
            if rec.get("fact")
        ]

    async def delete_fact(self, user_id: str, uuid: str) -> bool:
        """Hard-delete one relationship edge, scoped to the user's group so it can
        never touch another user's graph (§0.5 isolation)."""
        cypher = (
            "MATCH (:Entity)-[r:RELATES_TO {uuid: $uuid, group_id: $gid}]->(:Entity) "
            "DELETE r RETURN count(r) AS removed"
        )
        async with self._db.neo4j().session() as session:
            result = await session.run(cypher, uuid=uuid, gid=user_id)
            record = await result.single()
        return bool(record and record.get("removed", 0))

    async def delete_all_for_user(self, user_id: str) -> int:
        """Wipe the WHOLE knowledge graph for one user — every node (Entity, Episodic,
        Community…) and edge Graphiti wrote under this ``group_id`` (account deletion,
        GDPR-style). Group-scoped so it can never touch another user's graph (§0.5).
        DETACH DELETE removes each node's relationships with it. Returns nodes deleted."""
        cypher = (
            "MATCH (n) WHERE n.group_id = $gid "
            "WITH n LIMIT 50000 DETACH DELETE n RETURN count(n) AS removed"
        )
        removed = 0
        async with self._db.neo4j().session() as session:
            while True:  # batch so a huge graph never blows the transaction memory
                result = await session.run(cypher, gid=user_id)
                record = await result.single()
                n = int(record.get("removed", 0)) if record else 0
                removed += n
                if n == 0:
                    break
        return removed

    def _log_llm_usage(self, user_id: str, task: str) -> None:
        if self._ledger is None:
            return
        for usage in self._db.llm_usage.drain():
            prices = self._pricing.get(usage.model, {})
            cost = (
                usage.input_tokens * float(prices.get("input_per_mtok", 0.0))
                + usage.output_tokens * float(prices.get("output_per_mtok", 0.0))
            ) / 1_000_000
            self._ledger.log(
                CostEntry(
                    user_id=user_id,
                    component="llm",
                    provider="openrouter",
                    units={
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                    },
                    cost_usd=cost,
                    metadata=CostMetadata(task_id=task),
                )
            )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _neo_iso(value: object) -> str | None:
    """Normalize a Neo4j temporal (or None) to an ISO string."""
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else str(value)
