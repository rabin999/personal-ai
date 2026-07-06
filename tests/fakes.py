"""In-memory fakes for ports, shared across unit tests."""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any


class FakeDocStore:
    """In-memory DocStore covering the operations core modules use.

    ``aggregate`` interprets the $match/$group pipeline shapes the core
    builds (equality + $gte/$lte matches; $sum groups, optionally keyed by a
    ``$field`` path); real-Mongo behavior is covered by integration tests.
    """

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict[str, Any]]] = {}

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        doc = self.collections.get(collection, {}).get(doc_id)
        return dict(doc) if doc is not None else None

    async def put(self, collection: str, doc_id: str, doc: Mapping[str, Any]) -> None:
        stored = {k: v for k, v in doc.items() if k != "_id"} | {"_id": doc_id}
        self.collections.setdefault(collection, {})[doc_id] = stored

    async def find(
        self,
        collection: str,
        query: Mapping[str, Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        docs = [d for d in self.collections.get(collection, {}).values() if _matches(d, query)]
        return [dict(d) for d in docs[:limit]]

    async def insert(self, collection: str, doc: Mapping[str, Any]) -> str:
        doc_id = uuid.uuid4().hex
        await self.put(collection, doc_id, doc)
        return doc_id

    async def aggregate(
        self, collection: str, pipeline: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        docs = [dict(d) for d in self.collections.get(collection, {}).values()]
        for stage in pipeline:
            if "$match" in stage:
                docs = [d for d in docs if _matches(d, stage["$match"])]
            elif "$group" in stage:
                docs = _group(docs, stage["$group"])
            else:
                raise NotImplementedError(f"fake aggregate stage: {list(stage)}")
        return docs


def _lookup(doc: Mapping[str, Any], path: str) -> Any:
    value: Any = doc
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _matches(doc: Mapping[str, Any], query: Mapping[str, Any] | None) -> bool:
    for path, expected in (query or {}).items():
        actual = _lookup(doc, path)
        if isinstance(expected, Mapping):
            for op, operand in expected.items():
                if op == "$gte" and not (actual is not None and actual >= operand):
                    return False
                if op == "$lte" and not (actual is not None and actual <= operand):
                    return False
                if op not in ("$gte", "$lte"):
                    raise NotImplementedError(f"fake match operator: {op}")
        elif actual != expected:
            return False
    return True


def _group(docs: list[dict[str, Any]], spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    key_expr = spec["_id"]
    groups: dict[Any, dict[str, Any]] = {}
    for doc in docs:
        key = _lookup(doc, key_expr[1:]) if isinstance(key_expr, str) else key_expr
        row = groups.setdefault(key, {"_id": key})
        for field, agg in spec.items():
            if field == "_id":
                continue
            operand = agg["$sum"]
            increment = _lookup(doc, operand[1:]) if isinstance(operand, str) else operand
            row[field] = row.get(field, 0) + (increment or 0)
    return list(groups.values())
