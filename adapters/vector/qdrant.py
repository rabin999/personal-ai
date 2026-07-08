"""Adapter: Qdrant vector store (implements ports.vector_store.VectorStore, spec §5).

Owns embedding: dense semantic vectors via fastembed (local, CPU, free — no
paid call per memory write) and sparse BM25 term vectors via fastembed's
Qdrant/bm25 model, matching the IDF-modified sparse config created in §1.
Embedding runs in a worker thread so the event loop never blocks.
"""

import asyncio
from functools import cached_property

from fastembed import SparseEmbedding, SparseTextEmbedding, TextEmbedding
from qdrant_client import models

from adapters.db import DENSE_VECTOR, SPARSE_VECTOR, USER_ID_FIELD, Database
from ports.vector_store import VectorDoc, VectorHit

BM25_MODEL = "Qdrant/bm25"


class QdrantVectorStore:
    def __init__(self, db: Database, embedding_model: str) -> None:
        self._db = db
        self._model_name = embedding_model

    # Model init downloads/loads ONNX weights — lazy and cached so process
    # startup stays fast and tests that never embed pay nothing.
    @cached_property
    def _dense(self) -> TextEmbedding:
        return TextEmbedding(self._model_name)

    @cached_property
    def _sparse(self) -> SparseTextEmbedding:
        return SparseTextEmbedding(BM25_MODEL)

    async def upsert_texts(self, collection: str, docs: list[VectorDoc]) -> None:
        if not docs:
            return
        texts = [doc.text for doc in docs]
        dense_vectors, sparse_vectors = await asyncio.to_thread(self._embed_documents, texts)
        points = [
            models.PointStruct(
                id=doc.id,
                vector={
                    DENSE_VECTOR: dense,
                    SPARSE_VECTOR: models.SparseVector(
                        indices=sparse.indices.tolist(), values=sparse.values.tolist()
                    ),
                },
                payload={**doc.payload, "text": doc.text},
            )
            for doc, dense, sparse in zip(docs, dense_vectors, sparse_vectors, strict=True)
        ]
        await self._db.qdrant().upsert(collection_name=collection, points=points, wait=True)

    async def hybrid_search(
        self, collection: str, query_text: str, *, user_id: str, k: int = 6
    ) -> list[VectorHit]:
        dense_query, sparse_query = await asyncio.to_thread(self._embed_query, query_text)
        user_filter = models.Filter(
            must=[models.FieldCondition(key=USER_ID_FIELD, match=models.MatchValue(value=user_id))]
        )
        # Both legs fetch a wider candidate set, filtered per-leg; RRF fuses
        # ranks so each signal contributes regardless of score scales.
        response = await self._db.qdrant().query_points(
            collection_name=collection,
            prefetch=[
                models.Prefetch(
                    query=dense_query,
                    using=DENSE_VECTOR,
                    filter=user_filter,
                    limit=k * 3,
                ),
                models.Prefetch(
                    query=models.SparseVector(
                        indices=sparse_query.indices.tolist(),
                        values=sparse_query.values.tolist(),
                    ),
                    using=SPARSE_VECTOR,
                    filter=user_filter,
                    limit=k * 3,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=user_filter,
            limit=k,
            with_payload=True,
        )
        return [
            VectorHit(id=str(point.id), score=point.score, payload=dict(point.payload or {}))
            for point in response.points
        ]

    async def list_by_user(
        self, collection: str, *, user_id: str, limit: int = 100
    ) -> list[VectorHit]:
        user_filter = models.Filter(
            must=[models.FieldCondition(key=USER_ID_FIELD, match=models.MatchValue(value=user_id))]
        )
        points, _ = await self._db.qdrant().scroll(
            collection_name=collection,
            scroll_filter=user_filter,
            limit=limit,
            with_payload=True,
        )
        return [VectorHit(id=str(p.id), score=0.0, payload=dict(p.payload or {})) for p in points]

    async def delete(self, collection: str, doc_id: str, *, user_id: str) -> bool:
        # User-scoped: only delete the point if it belongs to this user (§0.5).
        user_filter = models.Filter(
            must=[
                models.HasIdCondition(has_id=[doc_id]),
                models.FieldCondition(key=USER_ID_FIELD, match=models.MatchValue(value=user_id)),
            ]
        )
        existing, _ = await self._db.qdrant().scroll(
            collection_name=collection, scroll_filter=user_filter, limit=1
        )
        if not existing:
            return False
        await self._db.qdrant().delete(
            collection_name=collection,
            points_selector=models.FilterSelector(filter=user_filter),
            wait=True,
        )
        return True

    async def delete_all_for_user(self, collection: str, *, user_id: str) -> None:
        """Delete EVERY point belonging to this user (account deletion). User-scoped
        so it can never touch another user's vectors (§0.5)."""
        user_filter = models.Filter(
            must=[models.FieldCondition(key=USER_ID_FIELD, match=models.MatchValue(value=user_id))]
        )
        await self._db.qdrant().delete(
            collection_name=collection,
            points_selector=models.FilterSelector(filter=user_filter),
            wait=True,
        )

    def _embed_documents(self, texts: list[str]) -> tuple[list[list[float]], list[SparseEmbedding]]:
        dense = [vector.tolist() for vector in self._dense.embed(texts)]
        sparse = list(self._sparse.embed(texts))
        return dense, sparse

    def _embed_query(self, text: str) -> tuple[list[float], SparseEmbedding]:
        dense = next(iter(self._dense.query_embed(text))).tolist()
        sparse = next(iter(self._sparse.query_embed(text)))
        return dense, sparse
