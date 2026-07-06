"""GS2 — Entity Resolution golden set runner (spec §8).

Seeds user entities, then resolves vague references and checks: a dominant
candidate resolves correctly; close/near-collision candidates trigger
disambiguation; unrelated phrases resolve to nothing. A silent wrong-resolution
is a critical failure. Runs against real Qdrant (integration).
"""

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from adapters.db import Database
from adapters.vector.qdrant import QdrantVectorStore
from config.settings import get_settings
from core.memory.entities import EntityResolver, is_ambiguous

pytestmark = pytest.mark.integration

GS = json.loads((Path(__file__).parent / "gs2_entities.json").read_text())


@pytest.fixture
async def resolver(db: Database) -> EntityResolver:
    user = f"gs2_{uuid.uuid4().hex[:10]}"
    entities = EntityResolver(QdrantVectorStore(db, get_settings().embedding_model))
    for e in GS["entities"]:
        await entities.index(user, e["type"], e["id"], e["name"], e["description"])
    entities._gs2_user = user  # type: ignore[attr-defined]
    return entities


@pytest.mark.parametrize("case", GS["cases"], ids=[c["id"] for c in GS["cases"]])
async def test_gs2_case(resolver: EntityResolver, case: dict[str, Any]) -> None:
    if "known_violation" in case:
        pytest.xfail(case["known_violation"])
    user = resolver._gs2_user  # type: ignore[attr-defined]
    candidates = await resolver.resolve(user, case["phrase"], k=3)

    if case.get("expect_no_resolution"):
        # No strong match — top candidate (if any) should be weak.
        assert not candidates or candidates[0].score < 0.5, (
            f"{case['id']}: unexpected resolution {[c.entity_id for c in candidates]}"
        )
        return

    assert candidates, f"{case['id']}: expected a resolution, got none"
    ambiguous = is_ambiguous(candidates)

    if case.get("expect_ambiguous"):
        assert ambiguous, (
            f"{case['id']}: expected disambiguation but top dominated: "
            f"{[(c.entity_id, round(c.score, 3)) for c in candidates]}"
        )
        ids = {c.entity_id for c in candidates[:2]}
        assert set(case["collision_between"]) <= ids or ids <= set(case["collision_between"])
    else:
        # Dominant candidate must be the expected one AND not ambiguous
        # (a close runner-up on a "dominant" case is a silent-wrong-resolution risk).
        assert candidates[0].entity_id == case["expect_resolves_to"], (
            f"{case['id']}: resolved to {candidates[0].entity_id}, "
            f"expected {case['expect_resolves_to']}"
        )
        assert not ambiguous, f"{case['id']}: dominant case is unexpectedly ambiguous"
