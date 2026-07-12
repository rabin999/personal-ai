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
    """Edge loop: load the stored pools into the in-memory catalog every ``interval_s``."""
    while True:
        try:
            pools = await store.load()
            if pools:
                catalog.apply({k: tuple(v) for k, v in pools.items()})
        except Exception:  # a bad load just means we keep the current pools
            logger.warning("phrase refresh failed; keeping current pools", exc_info=True)
        await asyncio.sleep(interval_s)
