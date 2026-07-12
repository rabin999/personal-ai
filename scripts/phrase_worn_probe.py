"""Real-call proof for the usage-driven worn-line refresh (§8.12 follow-up).

Simulates a line the user has worn out (use count over the threshold), then runs the REAL
worker refresh against the REAL model + REAL Redis and shows that ONLY that line is swapped
for a fresh, scrubber-valid one, the pool keeps its size and its other lines, and the use
count is reset. None of this is on the reply path — it's the worker loop.

Run:  uv run python -m scripts.phrase_worn_probe
"""

from __future__ import annotations

import asyncio

from core.phrases.catalog import PhraseCatalog
from core.phrases.defaults import DEFAULT_POOLS
from core.phrases.generator import _acceptable_spoken
from core.phrases.refresh import replace_worn_once
from tests.support.real_pipeline import RealTurns

POOL = "progress_lookup"
THRESHOLD = 10


async def main() -> None:
    turns = await RealTurns.build()
    p = turns.pipeline
    store, gen, spec_max = p.phrase_store, p.phrase_generator, 9

    # Start from a known pool and pretend the user has heard its first line 11 times.
    original = list(DEFAULT_POOLS[POOL])
    await store.save({POOL: original})
    cat = PhraseCatalog({POOL: tuple(original)})
    worn = original[0]
    await store.reset_uses([(POOL, ln) for ln in original])  # clean slate
    await store.bump_uses({(POOL, worn): THRESHOLD + 1})

    print(f"pool {POOL!r} before:")
    for ln in original:
        print(f"    {ln!r}{'   ← worn (11 uses)' if ln == worn else ''}")

    replaced = await replace_worn_once(gen, store, cat, threshold=THRESHOLD)

    new_pool = (await store.load())[POOL]
    print(f"\nreplaced {replaced} worn line(s). pool {POOL!r} after:")
    fresh = [ln for ln in new_pool if ln not in original]
    for ln in new_pool:
        tag = "   ← FRESH" if ln in fresh else ""
        print(f"    {ln!r}{tag}")

    remaining = await store.used_over(0)  # any counts left?
    print(f"\nuse counts remaining for {POOL!r}: {remaining.get(POOL, [])}")

    assert replaced == 1, "expected exactly the worn line to be replaced"
    assert worn not in new_pool, "worn line should be gone"
    assert len(new_pool) == len(original), "pool size must be preserved"
    assert all(ln in new_pool for ln in original[1:]), "other lines must be untouched"
    assert fresh and _acceptable_spoken(fresh[0], spec_max), "fresh line must pass the scrubber"
    assert (POOL, worn) not in {(POOL, ln) for ln in remaining.get(POOL, [])}, "count not reset"
    print("\nPASS — only the worn line was swapped, size preserved, fresh line clean, count reset.")


if __name__ == "__main__":
    asyncio.run(main())
