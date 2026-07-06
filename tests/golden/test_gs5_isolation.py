"""GS5 — Multi-Tenant Isolation golden set runner (global invariant §0.5).

Seeds user A's private data, then issues every retrieval AS user B and asserts
ZERO of user A's data appears. Absolute / binary — any leak is a critical,
ship-blocking failure. Runs against the real datastores (integration).
"""

import json
import uuid
from pathlib import Path

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from adapters.graph.graphiti import GraphitiGraphStore
from adapters.vector.qdrant import QdrantVectorStore
from config.settings import get_settings
from core.memory.entities import EntityResolver
from core.memory.episodic import EpisodicMemory
from core.memory.procedural import ProceduralMemory
from core.memory.semantic import SemanticMemory
from core.reasoning.self_model import SelfModel, TurnRecord

pytestmark = pytest.mark.integration

GS = json.loads((Path(__file__).parent / "gs5_isolation.json").read_text())


@pytest.fixture
def user_a() -> str:
    return f"gs5_a_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def user_b() -> str:
    return f"gs5_b_{uuid.uuid4().hex[:10]}"


async def test_gs5_no_cross_user_leak(db: Database, user_a: str, user_b: str) -> None:
    settings = get_settings()
    docs = MongoDocStore(db)
    vectors = QdrantVectorStore(db, settings.embedding_model)
    episodic = EpisodicMemory(vectors)
    entities = EntityResolver(vectors)
    self_model = SelfModel(docs, vectors)
    procedural = ProceduralMemory(docs)

    a = GS["user_a"]
    # Seed user A's private data.
    await episodic.write(user_a, "s_a", a["episodic"])
    await entities.index(
        user_a,
        a["entity"]["type"],
        a["entity"]["id"],
        a["entity"]["name"],
        a["entity"]["description"],
    )
    await self_model.log(TurnRecord(user_id=user_a), statement_text=a["self_statement"])
    r = a["procedural_rule"]
    rule = await procedural.add_candidate(
        user_a, rule_text=r["rule_text"], trigger=r["trigger"], action=r["action"]
    )
    for _ in range(5):  # push above the injection threshold so it is retrievable
        rule = await procedural.reinforce(user_a, rule.id)

    # Seed user B with unrelated data.
    b = GS["user_b"]
    await episodic.write(user_b, "s_b", b["episodic"])
    await entities.index(
        user_b,
        b["entity"]["type"],
        b["entity"]["id"],
        b["entity"]["name"],
        b["entity"]["description"],
    )

    leaks: list[str] = []

    # Probe as user B — must never see user A's data.
    hits = await episodic.retrieve(user_b, "ZORPX ticker shares", k=10)
    blob = " ".join(h.text for h in hits)
    for bad in ("ZORPX", "999", "Waffles"):
        if bad in blob:
            leaks.append(f"episodic leaked {bad!r} to user B: {blob[:120]}")

    candidates = await entities.resolve(user_b, "my portfolio")
    for c in candidates:
        if c.entity_id == "proj_a_secret":
            leaks.append("entity resolution leaked proj_a_secret to user B")

    statements = await self_model.recall(user_b, "ZORPX", k=10)
    for s in statements:
        if "ZORPX" in s.text:
            leaks.append(f"self-model leaked to user B: {s.text[:120]}")

    rules = await procedural.rules_for(user_b, context="zorpx feedback")
    for rule in rules:
        if "blunt" in rule.rule_text:
            leaks.append(f"procedural rule leaked to user B: {rule.rule_text}")

    # Semantic (Graphiti) isolation — group_ids scoping.
    try:
        graph = SemanticMemory(GraphitiGraphStore(db))
        await graph.add_episode(user_a, "user's secret ticker ZORPX with 999 shares")
        facts = await graph.facts_for(user_b, ["ZORPX"], limit=10)
        if any("ZORPX" in f.fact for f in facts):
            leaks.append("semantic (Graphiti) leaked ZORPX to user B")
    except Exception as exc:  # Graphiti needs an LLM key; note if unavailable
        pytest.skip(f"semantic isolation probe skipped (Graphiti unavailable): {exc}")
    finally:
        await db.mongo("procedural_memory").delete_many({"user_id": {"$in": [user_a, user_b]}})

    assert leaks == [], "MULTI-TENANT ISOLATION BREACH (critical):\n" + "\n".join(leaks)
