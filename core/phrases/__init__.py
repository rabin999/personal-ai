"""Dynamic phrase catalog (§8.12 follow-up): the short, curated spoken one-liners the
companion says on slow turns (interjections, progress nudges) and the greeting angles.

They ship with hand-written defaults (always safe, zero-cost, deterministic) and are
*periodically regenerated in the background* so they don't feel static forever. The live
turn NEVER waits on regeneration: it reads the current pool from an in-memory
`PhraseCatalog` (a pure dict lookup), a background worker regenerates the pools off-path
and writes them to a shared store, and the serving edge refreshes its in-memory copy from
that store on a slow tick. If regeneration is disabled or the store is empty/unreachable,
the defaults stand in — the mechanism can only ever make the phrases fresher, never break
them or slow the reply.
"""

from core.phrases.catalog import PhraseCatalog, PoolSpec
from core.phrases.defaults import DEFAULT_POOLS, POOL_SPECS

__all__ = ["DEFAULT_POOLS", "POOL_SPECS", "PhraseCatalog", "PoolSpec"]
