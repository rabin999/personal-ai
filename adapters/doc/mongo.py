"""Adapter: MongoDB document store (implements ports.doc_store.DocStore, spec §1)."""

from collections.abc import Mapping, Sequence
from typing import Any

from adapters.db import Database


class MongoDocStore:
    """Id-keyed document operations over the pooled Mongo client."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        return await self._db.mongo(collection).find_one({"_id": doc_id})

    async def put(self, collection: str, doc_id: str, doc: Mapping[str, Any]) -> None:
        replacement = {k: v for k, v in doc.items() if k != "_id"}
        await self._db.mongo(collection).replace_one({"_id": doc_id}, replacement, upsert=True)

    async def find(
        self,
        collection: str,
        query: Mapping[str, Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        cursor = self._db.mongo(collection).find(dict(query or {})).limit(limit)
        return [doc async for doc in cursor]

    async def insert(self, collection: str, doc: Mapping[str, Any]) -> str:
        result = await self._db.mongo(collection).insert_one(dict(doc))
        return str(result.inserted_id)

    async def aggregate(
        self, collection: str, pipeline: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        cursor = await self._db.mongo(collection).aggregate([dict(stage) for stage in pipeline])
        return [doc async for doc in cursor]
