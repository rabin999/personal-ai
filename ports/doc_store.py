"""Port: document store (MongoDB adapter) — config, profiles, projects, ledger, cost (spec §1).

Grows with the modules that use it; §2 (Config & User Profile) needs id-keyed
get/put and simple queries. Documents are plain mappings keyed by ``_id``.
"""

from collections.abc import Mapping
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
