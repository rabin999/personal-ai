"""Real-call proof for the dynamic phrase catalog (§8.12 follow-up).

Runs the REAL background regenerator against the REAL model and checks the two things that
matter:

  1. QUALITY (CLAUDE.md §6/§7): every regenerated SPOKEN line passes the exact same
     assistant-speak + slang scrubber the live reply runs — the background job can't smuggle
     "How can I help you?" or "dude" into what the companion says.
  2. NO LATENCY ON THE TURN: the live pick reads the freshly-applied pool as a pure in-memory
     lookup, measured in microseconds — regeneration (the model call) happened off to the side.

Run:  uv run python -m scripts.phrase_regen_probe
"""

from __future__ import annotations

import asyncio
import time

from core.phrases.defaults import POOL_SPECS
from core.phrases.generator import _acceptable_spoken
from tests.support.real_pipeline import RealTurns


async def main() -> None:
    turns = await RealTurns.build()
    p = turns.pipeline

    print("regenerating all phrase pools with the REAL model (off the reply path)…\n")
    t0 = time.monotonic()
    pools = await p.phrase_generator.regenerate()
    regen_ms = (time.monotonic() - t0) * 1000
    print(f"regeneration took {regen_ms:.0f} ms — this ran in the WORKER, not the turn.\n")

    specs = {s.name: s for s in POOL_SPECS}
    bad: list[str] = []
    for name, lines in pools.items():
        spec = specs[name]
        print(f"[{name}]  ({len(lines)} lines)")
        for ln in lines:
            ok = (not spec.spoken) or _acceptable_spoken(ln, spec.max_words)
            mark = "  ✓" if ok else "  ✗ FAILS SCRUBBER"
            if not ok:
                bad.append(f"{name}: {ln!r}")
            print(f"    {ln!r}{mark}")
        print()

    # Apply exactly what the background refresher would, then prove the live pick sees it —
    # and that the pick itself is instant (in-memory), adding nothing to the turn.
    p.phrases.apply({k: tuple(v) for k, v in pools.items()})
    t1 = time.perf_counter()
    for _ in range(10_000):
        p.phrases.get("progress_lookup")
    pick_us = (time.perf_counter() - t1) / 10_000 * 1e6
    sample = p.phrases.get("progress_lookup")
    print(f"live pick after refresh → {sample[0]!r}  (…{len(sample)} options)")
    print(f"hot-path get() cost: {pick_us:.2f} µs per pick (pure in-memory, no I/O)\n")

    assert not bad, "regenerated lines failed the live scrubber:\n  " + "\n  ".join(bad)
    print(f"QUALITY GATE PASSED — all {sum(len(v) for v in pools.values())} lines clean.")


if __name__ == "__main__":
    asyncio.run(main())
