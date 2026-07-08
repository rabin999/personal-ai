"""Port: document store (MongoDB adapter) — config, profiles, projects, ledger, cost (spec §1).

Grows with the modules that use it: §2 (Config & User Profile) needs id-keyed
get/put and simple queries; §3 (Cost Ledger) adds append + aggregation.
Documents are plain mappings keyed by ``_id``.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class DocStore(Protocol):
    """Minimal id-keyed document operations over named collections."""

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        """Return the document with ``_id == doc_id``, or None."""
        ...

    async def put(self, collection: str, doc_id: str, doc: Mapping[str, Any]) -> None:
        """Upsert ``doc`` under ``_id == doc_id`` (full replace)."""
        ...

    async def find(
        self,
        collection: str,
        query: Mapping[str, Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return documents matching ``query`` (all when None), up to ``limit``."""
        ...

    async def insert(self, collection: str, doc: Mapping[str, Any]) -> str:
        """Append ``doc`` with a generated ``_id``; return that id."""
        ...

    async def delete_many(self, collection: str, query: Mapping[str, Any]) -> int:
        """Delete every document matching ``query``; return how many were removed.
        Used by account deletion (GDPR-style wipe of all of a user's data)."""
        ...

    async def aggregate(
        self, collection: str, pipeline: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Run a $match/$group-style aggregation pipeline."""
        ...
