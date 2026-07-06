"""GS1 — Memory Retrieval golden set runner (spec §5 hybrid RRF).

Seeds episodic memory, runs each query through the real hybrid (dense+BM25→RRF)
retrieval, and checks the expected chunk is retrieved (recall@k) and — for
exact-keyword queries — ranks first (where BM25 should dominate). Aggregate
recall must clear 0.8. Runs against real Qdrant (integration).
"""

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from adapters.db import Database
from adapters.vector.qdrant import QdrantVectorStore
from config.settings import get_settings
from core.memory.episodic import EpisodicMemory

pytestmark = pytest.mark.integration

GS = json.loads((Path(__file__).parent / "gs1_memory.json").read_text())
K = 4


@pytest.fixture(scope="module")
async def seeded() -> tuple[EpisodicMemory, str]:
    settings = get_settings()
    db = Database(settings)
    await db.startup()
    user = f"gs1_{uuid.uuid4().hex[:10]}"
    episodic = EpisodicMemory(QdrantVectorStore(db, settings.embedding_model))
    await episodic.write(user, "s_gs1", GS["chunks"])
    return episodic, user


@pytest.mark.parametrize("case", GS["cases"], ids=[c["id"] for c in GS["cases"]])
async def test_gs1_case(seeded: tuple[EpisodicMemory, str], case: dict[str, Any]) -> None:
    episodic, user = seeded
    hits = await episodic.retrieve(user, case["query"], k=K)
    texts = [h.text for h in hits]

    found_rank = next(
        (i for i, t in enumerate(texts) if case["expect_keyword"].lower() in t.lower()), None
    )
    assert found_rank is not None, (
        f"{case['id']}: expected chunk (keyword {case['expect_keyword']!r}) not in top-{K}: {texts}"
    )
    if case.get("expect_rank_1"):
        assert found_rank == 0, (
            f"{case['id']}: exact-keyword query did not rank the expected chunk first "
            f"(rank {found_rank}); BM25 leg may be underweighted"
        )


async def test_gs1_aggregate_recall(seeded: tuple[EpisodicMemory, str]) -> None:
    episodic, user = seeded
    hit_count = 0
    for case in GS["cases"]:
        hits = await episodic.retrieve(user, case["query"], k=K)
        if any(case["expect_keyword"].lower() in h.text.lower() for h in hits):
            hit_count += 1
    recall = hit_count / len(GS["cases"])
    assert recall >= 0.8, f"GS1 aggregate recall@{K} = {recall:.2f} < 0.80"
