"""`PhraseCatalog` — the in-memory holder the live turn reads from.

The hot path (`_dynamic_ack`, `_emit_progress_ack`, the open greeting) calls `get(pool)`,
which is a pure dict lookup — NO I/O, NO await, no chance of blocking the reply. A background
refresher swaps in freshly-regenerated pools with `apply(...)`; the swap is atomic (a whole-dict
replace), so a reader always sees a complete, self-consistent set. Anything missing or rejected
falls back to the hand-written defaults, so `get` can never return empty.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from core.phrases.defaults import DEFAULT_POOLS, POOL_SPECS, PoolSpec

logger = logging.getLogger(__name__)

__all__ = ["PhraseCatalog", "PoolSpec"]


class PhraseCatalog:
    """Holds the current spoken-phrase pools, seeded with the defaults."""

    def __init__(self, pools: dict[str, tuple[str, ...]] | None = None) -> None:
        # A private copy so a caller's dict can't mutate us after construction.
        self._defaults: dict[str, tuple[str, ...]] = dict(DEFAULT_POOLS)
        self._pools: dict[str, tuple[str, ...]] = dict(self._defaults)
        # In-memory use counts, keyed (pool, line). Incremented on the hot path (a plain dict
        # bump — no I/O) and periodically DRAINED to the shared store by the edge refresher, so
        # the worker can refresh the lines the user has actually worn out. Kept here, not in the
        # store, precisely so counting never touches the network on the turn.
        self._uses: dict[tuple[str, str], int] = defaultdict(int)
        if pools:
            self.apply(pools)

    def get(self, name: str) -> tuple[str, ...]:
        """The current lines for a pool, or the hand-written default if none was regenerated.

        Pure, synchronous, in-memory — safe on the latency-critical filler path."""
        current = self._pools.get(name)
        if current:
            return current
        return self._defaults.get(name, ())

    def apply(self, pools: dict[str, tuple[str, ...]]) -> None:
        """Atomically swap in regenerated pools. Only known, non-empty pools are taken; an
        unknown name is ignored and an empty/absent one keeps its default — so a partial or
        malformed update can never blank a pool the live path depends on."""
        known = {spec.name for spec in POOL_SPECS}
        merged = dict(self._defaults)
        for name, lines in pools.items():
            if name not in known:
                logger.debug("phrase catalog: ignoring unknown pool %r", name)
                continue
            clean = tuple(ln for ln in lines if ln and ln.strip())
            if clean:
                merged[name] = clean
        self._pools = merged  # single-reference swap → readers never see a half-applied set

    def record_use(self, pool: str, line: str) -> None:
        """Count that ``line`` from ``pool`` was just spoken. A plain in-memory increment — safe
        on the hot path; the counts are flushed to the shared store off-path (see drain_uses)."""
        self._uses[(pool, line)] += 1

    def drain_uses(self) -> dict[tuple[str, str], int]:
        """Return the accumulated use counts and clear them (the edge flushes these to the store
        so the worker can act on them, then starts a fresh local tally)."""
        drained = dict(self._uses)
        self._uses.clear()
        return drained

    def snapshot(self) -> dict[str, list[str]]:
        """The current pools as plain lists — for the store to serialize."""
        return {name: list(lines) for name, lines in self._pools.items()}
