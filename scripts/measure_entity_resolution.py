"""E2 — entity resolution accuracy on ambiguous references, with and without the portfolio.

`"what's the LTP of OP?"` answered with the price of the **Optimism crypto token** even after
`OP` was correctly resolved to a NEPSE share in the user's portfolio (`SESSION_REPORT_F1-F6`
§F5, item 3). Two separate things must hold, and only one of them is entity resolution:

    1. RESOLUTION   `OP` → the seeded holding, not nothing and not the project
    2. PROPAGATION  that resolved entity reaches the search query

This script measures (1) against the real `EntityResolver` over real Qdrant. (2) is covered
deterministically by `tests/engine/test_e1_steps.py::test_search_query_is_built_from_the_
resolved_entity`.

The measurement is run twice per label:

    seeded    as `u_demo_001`, whose portfolio holds OP / SYPNL / NABIL
    unseeded  as a fresh user with no entities at all

An ambiguous ticker MUST resolve for the seeded user and MUST NOT resolve for the unseeded
one. A resolver that returns the holding for a user who does not hold it is a multi-tenant
leak; a resolver that returns nothing for a user who does is the SRC1 defect.

Usage:
    uv run python -m scripts.measure_entity_resolution
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "quality" / "entity_resolution_metrics.json"

SEEDED_USER = "u_demo_001"
ACCURACY_GATE = 0.95

# (reference, expected entity_id for the SEEDED user or None, why)
# `None` means "must resolve to nothing" — an unrelated phrase must not be forced onto a
# holding just because the vector space has room for it.
CASES: list[tuple[str, str | None, str]] = [
    # Exact tickers the user actually holds. Ambiguous on the open web; unambiguous here.
    ("OP", "op", "a NEPSE holding; on the open web it is the Optimism crypto token"),
    ("SYPNL", "sypnl", "a NEPSE holding; the F5 A/B fixture"),
    ("NABIL", "nabil", "a NEPSE holding"),
    ("op", "op", "case-insensitivity must not change the resolution"),
    # Natural phrasings a person actually uses.
    ("the LTP of OP", "op", "the ticker is embedded in a question"),
    ("how's OP doing", "op", "conversational phrasing"),
    ("my OP shares", "op", "possessive phrasing"),
    ("what did I pay for SYPNL", "sypnl", "past-tense phrasing"),
    # The project, not a holding. A near-collision: the project contains the holdings.
    ("my portfolio", "proj", "must prefer the PROJECT over a holding inside it"),
    ("my trading thing", "proj", "the design doc's own vague-reference example (§14.2)"),
    # Must resolve to nothing.
    ("my dog Trishul", None, "an unrelated entity the user never registered"),
    ("the weather in Kathmandu", None, "not an entity at all"),
    ("Ethereum", None, "a real ticker the user does NOT hold — must not snap to OP"),
    ("Optimism", None, "the crypto token OP is confused with — must not resolve"),
    ("hi", None, "a greeting is not a reference"),
]


def _matches(candidates: list, expected: str | None) -> bool:
    if expected is None:
        return not candidates
    if not candidates:
        return False
    top = candidates[0]
    if expected == "proj":
        return top.entity_type == "project"
    return top.entity_id == expected


async def main() -> int:
    from api.composition import build_pipeline
    from config.settings import get_settings
    from core.memory.entities import EntityResolver

    pipe = await build_pipeline(get_settings())
    resolver: EntityResolver = pipe.assembler._entities
    unseeded_user = f"u_unseeded_{uuid.uuid4().hex[:8]}"

    records = []
    seeded_hits = unseeded_hits = 0
    seeded_total = unseeded_total = 0
    try:
        for reference, expected, why in CASES:
            seeded = await resolver.resolve(SEEDED_USER, reference)
            unseeded = await resolver.resolve(unseeded_user, reference)

            seeded_ok = _matches(seeded, expected)
            # For an unseeded user, EVERY reference must resolve to nothing: they own no
            # entities. A hit here is a multi-tenant leak, not a resolution error.
            unseeded_ok = not unseeded

            seeded_hits += seeded_ok
            seeded_total += 1
            unseeded_hits += unseeded_ok
            unseeded_total += 1

            records.append(
                {
                    "reference": reference,
                    "expected": expected,
                    "why": why,
                    "seeded_ok": seeded_ok,
                    "unseeded_ok": unseeded_ok,
                    "seeded_top": (
                        f"{seeded[0].entity_id}({seeded[0].entity_type}) "
                        f"score={seeded[0].score:.2f}"
                        if seeded
                        else None
                    ),
                    "seeded_all": [f"{c.entity_id}:{c.score:.2f}" for c in seeded],
                    "unseeded_all": [f"{c.entity_id}:{c.score:.2f}" for c in unseeded],
                }
            )
    finally:
        await pipe.aclose()

    seeded_acc = seeded_hits / seeded_total
    unseeded_acc = unseeded_hits / unseeded_total

    print(f"\n{'reference':28s} {'expected':10s} {'seeded resolution':38s} {'ok':4s} {'iso':4s}")
    print("-" * 92)
    for r in records:
        print(
            f"{r['reference'][:26]:28s} {str(r['expected'])[:8]:10s} "
            f"{str(r['seeded_top'])[:36]:38s} "
            f"{'✓' if r['seeded_ok'] else 'MISS':4s} {'✓' if r['unseeded_ok'] else 'LEAK':4s}"
        )

    print(
        f"\nseeded accuracy    {seeded_acc:.3f}  "
        f"[{'PASS' if seeded_acc >= ACCURACY_GATE else 'FAIL'} vs {ACCURACY_GATE}]"
    )
    print(
        f"unseeded isolation {unseeded_acc:.3f}  "
        f"[{'PASS' if unseeded_acc == 1.0 else 'FAIL'} vs 1.0 — any leak is ship-blocking]"
    )

    wrong = [r for r in records if not r["seeded_ok"]]
    if wrong:
        print(f"\n{len(wrong)} misresolution(s):")
        for r in wrong:
            print(f"  {r['reference']!r} expected {r['expected']!r}, got {r['seeded_all']}")
            print(f"      ({r['why']})")

    leaks = [r for r in records if not r["unseeded_ok"]]
    if leaks:
        print(f"\n{len(leaks)} ISOLATION LEAK(S) — a user with no entities resolved one:")
        for r in leaks:
            print(f"  {r['reference']!r} -> {r['unseeded_all']}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "seeded_user": SEEDED_USER,
                "n": len(records),
                "accuracy_gate": ACCURACY_GATE,
                "seeded_accuracy": round(seeded_acc, 4),
                "seeded_accuracy_pass": seeded_acc >= ACCURACY_GATE,
                "unseeded_isolation": round(unseeded_acc, 4),
                "unseeded_isolation_pass": unseeded_acc == 1.0,
                "records": records,
            },
            indent=2,
        )
    )
    print(f"\nwrote {OUT.relative_to(ROOT)}")
    return 0 if (seeded_acc >= ACCURACY_GATE and unseeded_acc == 1.0) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
