"""The two background loops that keep the phrase catalog fresh WITHOUT ever touching the
conversation path.

- `regenerate_forever` runs in the WORKER process: periodically rewrites the pools (one cheap
  LLM call) and writes them to the shared store. This is where the cost + latency live — off
  the reply path entirely.
- `refresh_forever` runs on the SERVING EDGE: periodically loads the stored pools into the
  in-memory `PhraseCatalog` the live turn reads. A cheap store GET + an atomic dict swap; the
  filler pick itself never awaits either loop.

Both are defensive: any failure is logged and the loop continues, so the worst case is simply
that the catalog keeps its current (or default) pools. Neither loop is on the turn path, so a
slow or failing one can never delay a reply.
"""

from __future__ import annotations

import asyncio
import logging

from core.phrases.catalog import PhraseCatalog
from core.phrases.generator import PhraseGenerator
from ports.phrase_store import PhraseStore

logger = logging.getLogger(__name__)


async def regenerate_once(
    generator: PhraseGenerator, store: PhraseStore, catalog: PhraseCatalog
) -> int:
    """Regenerate the pools, persist them, and apply them locally. Returns how many pools were
    refreshed (0 = provider hiccup / nothing acceptable → current pools stand)."""
    pools = await generator.regenerate()
    if not pools:
        return 0
    await store.save(pools)
    catalog.apply({k: tuple(v) for k, v in pools.items()})
    return len(pools)


async def regenerate_forever(
    generator: PhraseGenerator,
    store: PhraseStore,
    catalog: PhraseCatalog,
    interval_s: float,
) -> None:
    """Worker loop: regenerate the pools every ``interval_s`` seconds, off the reply path."""
    while True:
        try:
            n = await regenerate_once(generator, store, catalog)
            if n:
                logger.info("phrase regen: refreshed %d pool(s)", n)
        except Exception:  # never let the regen loop die
            logger.exception("phrase regen loop failed; will retry next tick")
        await asyncio.sleep(interval_s)


async def refresh_forever(store: PhraseStore, catalog: PhraseCatalog, interval_s: float) -> None:
    """Edge loop: every ``interval_s`` FLUSH the in-memory use counts to the shared store (so the
    worker can act on them), then LOAD the latest pools into the in-memory catalog. Both are cheap
    Redis ops on a background task — the live filler pick never touches either."""
    while True:
        try:
            uses = catalog.drain_uses()
            if uses:
                await store.bump_uses(uses)
            pools = await store.load()
            if pools:
                catalog.apply({k: tuple(v) for k, v in pools.items()})
        except Exception:  # a bad tick just means we keep the current pools / retry next time
            logger.warning("phrase refresh failed; keeping current pools", exc_info=True)
        await asyncio.sleep(interval_s)


async def replace_worn_once(
    generator: PhraseGenerator, store: PhraseStore, catalog: PhraseCatalog, threshold: int
) -> int:
    """Refresh the lines the user has WORN OUT: for each line used more than ``threshold`` times,
    generate a fresh replacement, swap it into the pool (keeping the fresher lines and the pool's
    size), persist + apply, and reset that line's count. Returns how many lines were replaced.

    Lines with no available replacement (a provider hiccup) are LEFT in place and their counts are
    NOT reset, so they're retried next tick rather than silently dropped."""
    worn = await store.used_over(threshold)
    if not worn:
        return 0
    current = await store.load() or {}
    replaced = 0
    reset_keys: list[tuple[str, str]] = []
    for pool, worn_lines in worn.items():
        pool_lines = list(current.get(pool) or catalog.get(pool))
        # Only worn lines still present in the pool are worth replacing.
        worn_present = [ln for ln in worn_lines if ln in pool_lines]
        # Stale counts for lines no longer in the pool → just clear them.
        reset_keys += [(pool, ln) for ln in worn_lines if ln not in pool_lines]
        if not worn_present:
            continue
        fresh = await generator.regenerate_replacements(pool, keep=pool_lines, n=len(worn_present))
        if not fresh:
            continue  # keep the worn lines + their counts; retry next tick
        to_remove = worn_present[: len(fresh)]  # swap 1:1 so the pool size is unchanged
        new_pool = [ln for ln in pool_lines if ln not in to_remove] + fresh
        current[pool] = new_pool
        reset_keys += [(pool, ln) for ln in to_remove]
        replaced += len(fresh)
    if replaced:
        await store.save(current)
        catalog.apply({k: tuple(v) for k, v in current.items()})
    if reset_keys:
        await store.reset_uses(reset_keys)
    return replaced


async def refresh_worn_forever(
    generator: PhraseGenerator,
    store: PhraseStore,
    catalog: PhraseCatalog,
    threshold: int,
    interval_s: float,
) -> None:
    """Worker loop: every ``interval_s`` replace the lines the user has worn out (usage-driven)."""
    while True:
        try:
            n = await replace_worn_once(generator, store, catalog, threshold)
            if n:
                logger.info("phrase refresh: replaced %d worn line(s)", n)
        except Exception:  # never let the loop die
            logger.exception("worn-line refresh failed; will retry next tick")
        await asyncio.sleep(interval_s)
