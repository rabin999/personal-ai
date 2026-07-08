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

    async def delete_many(self, collection: str, query: Mapping[str, Any]) -> int:
        col = self.collections.get(collection, {})
        gone = [k for k, d in col.items() if _matches(d, query)]
        for k in gone:
            del col[k]
        return len(gone)

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


class FakeVectorStore:
    """Functional in-memory VectorStore: token-overlap scoring, user-filtered."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    async def upsert_texts(self, collection: str, docs: list[Any]) -> None:
        for doc in docs:
            self.docs[f"{collection}:{doc.id}"] = {
                "collection": collection,
                "id": doc.id,
                "text": doc.text,
                "payload": {**doc.payload, "text": doc.text},
            }

    async def hybrid_search(
        self, collection: str, query_text: str, *, user_id: str, k: int = 6
    ) -> list[Any]:
        from ports.vector_store import VectorHit

        query_words = _tokens(query_text)
        scored = []
        for doc in self.docs.values():
            if doc["collection"] != collection:
                continue
            if doc["payload"].get("user_id") != user_id:
                continue
            overlap = len(query_words & _tokens(doc["text"]))
            if overlap:
                scored.append((overlap, doc))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            VectorHit(id=doc["id"], score=float(score), payload=dict(doc["payload"]))
            for score, doc in scored[:k]
        ]

    async def list_by_user(self, collection: str, *, user_id: str, limit: int = 100) -> list[Any]:
        from ports.vector_store import VectorHit

        out = [
            VectorHit(id=doc["id"], score=0.0, payload=dict(doc["payload"]))
            for doc in self.docs.values()
            if doc["collection"] == collection and doc["payload"].get("user_id") == user_id
        ]
        return out[:limit]

    async def delete(self, collection: str, doc_id: str, *, user_id: str) -> bool:
        key = f"{collection}:{doc_id}"
        doc = self.docs.get(key)
        if doc is None or doc["payload"].get("user_id") != user_id:
            return False
        del self.docs[key]
        return True

    async def delete_all_for_user(self, collection: str, *, user_id: str) -> None:
        gone = [
            k
            for k, d in self.docs.items()
            if k.startswith(f"{collection}:") and d["payload"].get("user_id") == user_id
        ]
        for k in gone:
            del self.docs[k]


def _tokens(text: str) -> set[str]:
    return {w.strip(".,!?:;'\"()").lower() for w in text.split() if len(w) > 2}


class FakeGraphStore:
    """In-memory GraphStore: facts added directly, token-overlap retrieval."""

    def __init__(self) -> None:
        self.facts_by_user: dict[str, list[Any]] = {}
        self.episodes: list[dict[str, Any]] = []

    def seed_fact(self, user_id: str, fact: Any) -> None:
        self.facts_by_user.setdefault(user_id, []).append(fact)

    async def setup(self) -> None: ...

    async def add_episode(self, user_id: str, text: str, timestamp: str | None = None) -> None:
        self.episodes.append({"user_id": user_id, "text": text, "timestamp": timestamp})

    async def search_facts(self, user_id: str, query: str, limit: int = 10) -> list[Any]:
        query_words = _tokens(query)
        matches = [
            fact for fact in self.facts_by_user.get(user_id, []) if _tokens(fact.fact) & query_words
        ]
        return matches[:limit]

    async def list_facts(self, user_id: str, limit: int = 200) -> list[Any]:
        return list(self.facts_by_user.get(user_id, []))[:limit]

    async def delete_fact(self, user_id: str, uuid: str) -> bool:
        facts = self.facts_by_user.get(user_id, [])
        for i, fact in enumerate(facts):
            if getattr(fact, "uuid", None) == uuid:
                facts.pop(i)
                return True
        return False


class FakeLLM:
    """Scriptable LLM: pops queued response texts; records every call."""

    def __init__(self, responses: "Sequence[str | Exception] | None" = None) -> None:
        self.responses: list[str | Exception] = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.default_text = "okay!"

    async def complete(
        self,
        user_id: str,
        messages: Any,
        tier: str = "moderate",
        *,
        response_format: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        temperature: float | None = None,
        reasoning: Mapping[str, Any] | None = None,
        cache_prefix: str = "",
        purpose: str = "",
    ) -> Any:
        from ports.llm import CompletionResult

        self.calls.append(
            {
                "user_id": user_id,
                "messages": list(messages),
                "tier": tier,
                "response_format": response_format,
                "session_id": session_id,
                "model": model,
            }
        )
        text = self.responses.pop(0) if self.responses else self.default_text
        if isinstance(text, Exception):
            raise text
        return CompletionResult(
            text=text, model="fake/model", input_tokens=50, output_tokens=25, cost_usd=0.0002
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    def route(self, complexity: str) -> str:
        return "fake/model"

    def fast_model_choices(self) -> list[str]:
        return ["fake/fast-a", "fake/fast-b"]
