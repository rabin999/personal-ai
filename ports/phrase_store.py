"""Port: shared phrase-catalog store (§8.12 follow-up).

The background regenerator (worker process) `save`s a freshly-generated catalog; the serving
edge `load`s it on a slow tick to refresh its in-memory `PhraseCatalog`. A tiny key/value
contract — one global document — implemented over Redis. Kept a port so the store is swappable
and `core/` never imports the adapter.
"""

from __future__ import annotations

from typing import Protocol


class PhraseStore(Protocol):
    async def load(self) -> dict[str, list[str]] | None:
        """The currently-stored pools (name → lines), or None if nothing has been saved yet
        or the store is unreachable — the caller then keeps its defaults."""
        ...

    async def save(self, pools: dict[str, list[str]]) -> None:
        """Persist the regenerated pools as the new shared catalog."""
        ...
