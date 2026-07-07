"""Unit tests for durable tool-result persistence + recall (§13 / brief §5.2)."""

from typing import Any

from core.tools.dispatcher import ToolCall, ToolDispatcher
from core.tools.registry import ToolContext, ToolRegistry, ToolSpec
from core.tools.results import TOOL_RESULTS_COLLECTION, ToolResultStore
from tests.fakes import FakeDocStore

USER_A = "u_demo_001"
USER_B = "u_demo_002"


class _NoQueue:
    async def enqueue(self, **kwargs: Any) -> str:  # pragma: no cover - unused here
        raise NotImplementedError


async def test_store_records_and_returns_newest_first_user_scoped() -> None:
    store = ToolResultStore(FakeDocStore())
    await store.record(
        user_id=USER_A,
        session_id="s1",
        tool_id="web_search",
        args={"query": "nepse news"},
        output={"summary": "Market up 2%."},
    )
    await store.record(
        user_id=USER_A,
        session_id="s1",
        tool_id="web_search",
        args={"query": "weather"},
        output={"summary": "Sunny."},
    )
    await store.record(
        user_id=USER_B,
        session_id="s2",
        tool_id="web_search",
        args={"query": "secret"},
        output={"summary": "B only."},
    )

    latest = await store.latest(USER_A, tool="web_search")
    assert [d["query"] for d in latest] == ["weather", "nepse news"]  # newest first
    # §0.5: user A never sees user B's stored results.
    assert all(d["user_id"] == USER_A for d in latest)
    assert all("B only" not in str(d["output"]) for d in latest)


async def test_dispatcher_persists_inline_tool_results() -> None:
    docs = FakeDocStore()
    store = ToolResultStore(docs)
    registry = ToolRegistry()

    async def news(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return {"summary": f"headline for {args['query']}"}

    registry.register(
        ToolSpec(id="news", description="news", type="readonly", latency_class="fast"), news
    )
    dispatcher = ToolDispatcher(registry, _NoQueue(), results=store)  # type: ignore[arg-type]

    await dispatcher.dispatch(
        ToolCall(tool_id="news", args={"query": "eclipse"}),
        ToolContext(user_id=USER_A, session_id="s1"),
    )
    rows = await docs.find(TOOL_RESULTS_COLLECTION, {"user_id": USER_A})
    assert len(rows) == 1
    assert rows[0]["tool"] == "news" and rows[0]["query"] == "eclipse"
    assert "eclipse" in str(rows[0]["output"])


async def test_recall_tool_returns_stored_results() -> None:
    # The recall_tool_result tool resolves "what was that news?" against the store.
    from core.memory.entities import EntityResolver
    from core.memory.episodic import EpisodicMemory
    from core.memory.semantic import SemanticMemory
    from core.profile import ProfileService
    from core.projects.service import ProjectService
    from core.tools.builtin.core_tools import register_core_tools
    from core.tools.web_search import WebSearch
    from tests.fakes import FakeGraphStore, FakeLLM, FakeVectorStore
    from tests.unit.test_projects import _StubSearchProvider

    docs = FakeDocStore()
    store = ToolResultStore(docs)
    await store.record(
        user_id=USER_A,
        session_id="s1",
        tool_id="web_search",
        args={"query": "top news"},
        output={"summary": "Two distinct headlines."},
    )
    registry = ToolRegistry()
    register_core_tools(
        registry,
        episodic=EpisodicMemory(FakeVectorStore()),
        semantic=SemanticMemory(FakeGraphStore()),
        web_search=WebSearch(docs, FakeLLM(), _StubSearchProvider()),
        profiles=ProfileService(docs),
        projects=ProjectService(docs, EntityResolver(FakeVectorStore())),
        results=store,
    )
    _, handler = registry.get("recall_tool_result")
    out = await handler({"tool": "web_search"}, ToolContext(user_id=USER_A, session_id="s1"))
    assert out["results"]
    assert "Two distinct headlines" in str(out["results"][0]["output"])
