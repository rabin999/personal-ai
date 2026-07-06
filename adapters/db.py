"""Database Layer (spec §1): pooled clients + thin handles for the three stores.

All other modules get store handles from here — never construct raw driver
connections themselves. One ``Database`` instance is created at startup (the
composition root) and reused for the process lifetime; every driver below
pools connections internally, so handing out handles is free.

``core/`` never imports this module (hexagonal boundary): core modules reach
storage through their own ports (§5, §6, ...), whose adapters are built on
top of this layer.
"""

import asyncio
from typing import Any, TypedDict

import httpx
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.llm_client import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from neo4j import AsyncDriver, AsyncGraphDatabase
from openai import AsyncOpenAI
from pymongo import AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from qdrant_client import AsyncQdrantClient, models

from adapters.graph.embedder import FastembedEmbedder
from adapters.llm.usage import LLMUsageRecorder
from config.settings import Settings

# episodic/entities are fixed by spec §1 rule 3; self_statements is the
# Qdrant namespace §9 (Self-Model) reserves for the system's own prior
# statements. Vector names are shared across all hybrid-search collections.
EPISODIC_COLLECTION = "episodic"
ENTITIES_COLLECTION = "entities"
SELF_STATEMENTS_COLLECTION = "self_statements"
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "sparse"
USER_ID_FIELD = "user_id"

_QDRANT_COLLECTIONS = (EPISODIC_COLLECTION, ENTITIES_COLLECTION, SELF_STATEMENTS_COLLECTION)


class StoreHealth(TypedDict):
    mongo: bool
    qdrant: bool
    graph: bool


class Database:
    """Connection clients + thin repository handles (spec §1 interface)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._mongo: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            settings.mongo_uri,
            serverSelectionTimeoutMS=5_000,
            connectTimeoutMS=5_000,
        )
        # check_compatibility triggers a blocking version probe in __init__;
        # healthcheck() covers reachability instead.
        self._qdrant = AsyncQdrantClient(
            url=settings.qdrant_url, timeout=5, check_compatibility=False
        )
        self._neo4j: AsyncDriver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        # Built lazily: Graphiti spawns background index work and needs LLM
        # client config, which only §6 (Semantic Memory) actually uses.
        self._graphiti: Graphiti | None = None
        # Token usage from Graphiti's internal LLM calls; the graph adapter
        # drains this into the Cost Ledger after each operation (§0.5).
        self.llm_usage = LLMUsageRecorder()

    # ── spec §1 interface ────────────────────────────────────────────────

    def mongo(self, collection: str) -> AsyncCollection[dict[str, Any]]:
        """Configured async collection handle from the pooled client."""
        return self._mongo[self._settings.mongo_db][collection]

    def qdrant(self) -> AsyncQdrantClient:
        """The pooled Qdrant client."""
        return self._qdrant

    def graphiti(self) -> Graphiti:
        """Configured Graphiti instance (spec §6 wiring).

        LLM extraction goes through OpenRouter via the OpenAI-compatible
        generic client in json_object mode (strict json_schema is rejected —
        Graphiti's schemas lack additionalProperties:false);
        embeddings are local fastembed (no OpenRouter embeddings endpoint);
        token usage is recorded on the shared HTTP client for cost logging.
        """
        if self._graphiti is None:
            settings = self._settings
            if not settings.open_router_api_key:
                raise RuntimeError(
                    "OPEN_ROUTER_API_KEY is not set — Graphiti needs it for "
                    "LLM extraction calls (see .env.example)"
                )
            llm_config = LLMConfig(
                api_key=settings.open_router_api_key,
                base_url=settings.open_router_base_url,
                model=settings.graphiti_llm_model,
                small_model=settings.graphiti_small_model,
                # Extraction is a parsing task: keep it deterministic so the
                # same conversation yields the same graph.
                temperature=0,
            )
            openai_client = AsyncOpenAI(
                api_key=settings.open_router_api_key,
                base_url=settings.open_router_base_url,
                http_client=httpx.AsyncClient(
                    event_hooks={"response": [self.llm_usage.on_response]}
                ),
            )
            self._graphiti = Graphiti(
                graph_driver=Neo4jDriver(
                    uri=settings.neo4j_uri,
                    user=settings.neo4j_user,
                    password=settings.neo4j_password,
                ),
                llm_client=OpenAIGenericClient(
                    config=llm_config, client=openai_client, structured_output_mode="json_object"
                ),
                embedder=FastembedEmbedder(settings.embedding_model),
                cross_encoder=OpenAIRerankerClient(config=llm_config, client=openai_client),
            )
        return self._graphiti

    async def healthcheck(self) -> StoreHealth:
        """Ping all three stores concurrently; never raises."""
        mongo_ok, qdrant_ok, graph_ok = await asyncio.gather(
            self._ping_mongo(), self._ping_qdrant(), self._ping_graph()
        )
        return StoreHealth(mongo=mongo_ok, qdrant=qdrant_ok, graph=graph_ok)

    # ── startup / lifecycle (spec §1 rules 2 and 3) ──────────────────────

    async def startup(self) -> None:
        """Fail loudly if any store is unreachable; ensure Qdrant collections."""
        health = await self.healthcheck()
        if not all(health.values()):
            unreachable = ", ".join(name for name, ok in health.items() if not ok)
            raise RuntimeError(f"datastores unreachable at startup: {unreachable}")
        await self.ensure_qdrant_collections()

    async def ensure_qdrant_collections(self) -> None:
        """Create `episodic`/`entities` (dense+sparse) with a user_id index if absent."""
        for name in _QDRANT_COLLECTIONS:
            if not await self._qdrant.collection_exists(name):
                await self._qdrant.create_collection(
                    collection_name=name,
                    vectors_config={
                        DENSE_VECTOR: models.VectorParams(
                            size=self._settings.embedding_dim,
                            distance=models.Distance.COSINE,
                        )
                    },
                    sparse_vectors_config={
                        # IDF modifier => BM25-style scoring for the sparse leg
                        # of hybrid search (§5).
                        SPARSE_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)
                    },
                )
            # Multi-tenant isolation invariant: every search filters on
            # user_id, so it must be a payload index (filtered HNSW).
            await self._qdrant.create_payload_index(
                collection_name=name,
                field_name=USER_ID_FIELD,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )

    async def aclose(self) -> None:
        await asyncio.gather(self._mongo.close(), self._qdrant.close(), self._neo4j.close())

    # ── pings ────────────────────────────────────────────────────────────

    async def _ping_mongo(self) -> bool:
        try:
            await self._mongo.admin.command("ping")
        except Exception:
            return False
        return True

    async def _ping_qdrant(self) -> bool:
        try:
            await self._qdrant.get_collections()
        except Exception:
            return False
        return True

    async def _ping_graph(self) -> bool:
        try:
            await self._neo4j.verify_connectivity()
        except Exception:
            return False
        return True
