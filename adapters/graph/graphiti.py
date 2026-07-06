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
        reference_time = (
            datetime.fromisoformat(timestamp) if timestamp else datetime.now(UTC)
        )
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
            )
            for edge in edges
        ]

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
