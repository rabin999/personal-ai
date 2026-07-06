"""Projects (spec §16): long-lived user workspaces with typed blueprints.

Types are blueprints (seeded config), instances are user data. A type's
actions are registered as §13 tools dynamically — only once an instance
exists (rule 1). Derived metrics are computed from the append-only ledger,
never stored (rule 2). Insights are consent-gated: computed and stored as
pending, spoken only after the user says yes, always with the type's
domain caveat (rule 3).
"""

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.memory.entities import EntityResolver
from core.tools.registry import ToolContext, ToolRegistry, ToolSpec
from ports.doc_store import DocStore
from ports.llm import LLM, LLMUnavailable

logger = logging.getLogger(__name__)

PROJECT_TYPES_COLLECTION = "project_types"
PROJECTS_COLLECTION = "projects"
LEDGER_ENTRIES_COLLECTION = "ledger_entries"
PENDING_INSIGHTS_COLLECTION = "pending_insights"

_RECENT_ENTRIES = 5

_INSIGHT_INSTRUCTIONS = (
    "You observe a user's project metrics. State ONE short factual "
    "observation (1-2 spoken sentences) grounded ONLY in the numbers given. "
    "No advice, no predictions, no diagnosis — correlation is not causation. "
    "Return plain text only."
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Project(BaseModel):
    id: str
    user_id: str
    type: str
    name: str
    created_at: str


class LedgerEntry(BaseModel):
    id: str
    project_id: str
    user_id: str
    data: dict[str, Any]
    timestamp: str


class ProjectState(BaseModel):
    metrics: dict[str, Any]
    recent_entries: list[LedgerEntry]
    open_tasks: list[str] = Field(default_factory=list)


class Insight(BaseModel):
    id: str
    project_id: str
    user_id: str
    text: str
    caveat: str
    status: Literal["pending", "delivered", "dismissed"] = "pending"
    created_at: str


class ProjectNotFound(KeyError):
    pass


class ProjectService:
    def __init__(
        self,
        docs: DocStore,
        entities: EntityResolver,
        registry: ToolRegistry | None = None,
        llm: LLM | None = None,
    ) -> None:
        self._docs = docs
        self._entities = entities
        self._registry = registry
        self._llm = llm

    # ── lifecycle ────────────────────────────────────────────────────────

    async def create(self, user_id: str, type_id: str, name: str) -> Project:
        blueprint = await self._blueprint(type_id)
        project = Project(
            id=f"proj_{uuid.uuid4().hex[:12]}",
            user_id=user_id,
            type=type_id,
            name=name,
            created_at=_now(),
        )
        doc = project.model_dump()
        doc["_id"] = doc.pop("id")
        await self._docs.put(PROJECTS_COLLECTION, project.id, doc)
        # Rule 4 / §8: searchable pointer, updated on create or rename.
        await self._entities.index(
            user_id, "project", project.id, name, str(blueprint.get("description", type_id))
        )
        self._register_type_tools(type_id, blueprint)
        return project

    async def rename(self, project_id: str, user_id: str, name: str) -> Project:
        project = await self._project(project_id, user_id)
        project.name = name
        doc = project.model_dump()
        doc["_id"] = doc.pop("id")
        await self._docs.put(PROJECTS_COLLECTION, project.id, doc)
        blueprint = await self._blueprint(project.type)
        await self._entities.index(
            user_id, "project", project.id, name, str(blueprint.get("description", project.type))
        )
        return project

    async def sync_tool_registrations(self) -> None:
        """Register action tools for every type that has at least one instance."""
        projects = await self._docs.find(PROJECTS_COLLECTION, {}, limit=1000)
        for type_id in {p["type"] for p in projects}:
            try:
                blueprint = await self._blueprint(type_id)
            except ProjectNotFound:
                continue
            self._register_type_tools(type_id, blueprint)

    # ── ledger + state ───────────────────────────────────────────────────

    async def log_entry(
        self, project_id: str, user_id: str, data: Mapping[str, Any]
    ) -> LedgerEntry:
        project = await self._project(project_id, user_id)
        entry = LedgerEntry(
            id=str(uuid.uuid4()),
            project_id=project.id,
            user_id=user_id,
            data=dict(data),
            timestamp=_now(),
        )
        doc = entry.model_dump()
        doc["_id"] = doc.pop("id")
        await self._docs.put(LEDGER_ENTRIES_COLLECTION, entry.id, doc)
        return entry

    async def state(self, project_id: str, user_id: str) -> ProjectState:
        project = await self._project(project_id, user_id)
        entries = await self._entries(project_id, user_id)
        metrics = _finance_metrics(entries) if project.type == "finance_portfolio" else {}
        metrics["entry_count"] = len(entries)
        return ProjectState(metrics=metrics, recent_entries=entries[-_RECENT_ENTRIES:])

    async def project_context(self, user_id: str, entity_id: str) -> str | None:
        """§10 step 6: canonical project data for prompt assembly."""
        try:
            project = await self._project(entity_id, user_id)
        except ProjectNotFound:
            return None
        state = await self.state(entity_id, user_id)
        lines = [f"Project: {project.name} ({project.type})", f"Metrics: {state.metrics}"]
        if state.recent_entries:
            lines.append("Recent entries:")
            lines += [f"- {e.timestamp[:10]}: {e.data}" for e in state.recent_entries]
        return "\n".join(lines)

    # ── consent-gated insight (rule 3) ───────────────────────────────────

    async def run_insight(self, project_id: str, user_id: str) -> Insight | None:
        project = await self._project(project_id, user_id)
        state = await self.state(project_id, user_id)
        if state.metrics.get("entry_count", 0) == 0:
            return None
        blueprint = await self._blueprint(project.type)
        text = await self._compose_insight(user_id, project, state)
        if text is None:
            return None
        insight = Insight(
            id=str(uuid.uuid4()),
            project_id=project_id,
            user_id=user_id,
            text=text,
            caveat=str(blueprint.get("caveat", "")),
            created_at=_now(),
        )
        doc = insight.model_dump()
        doc["_id"] = doc.pop("id")
        await self._docs.put(PENDING_INSIGHTS_COLLECTION, insight.id, doc)
        return insight

    async def pending_insight(self, project_id: str, user_id: str) -> Insight | None:
        docs = await self._docs.find(
            PENDING_INSIGHTS_COLLECTION,
            {"project_id": project_id, "user_id": user_id, "status": "pending"},
        )
        if not docs:
            return None
        docs.sort(key=lambda d: d["created_at"], reverse=True)
        return _insight_from_doc(docs[0])

    async def consent_and_deliver(self, insight_id: str, user_id: str) -> str:
        """User said yes: mark delivered, return text with the domain caveat."""
        doc = await self._docs.get(PENDING_INSIGHTS_COLLECTION, insight_id)
        if doc is None or doc.get("user_id") != user_id:
            raise ProjectNotFound(insight_id)
        insight = _insight_from_doc(doc)
        insight.status = "delivered"
        updated = insight.model_dump()
        updated["_id"] = updated.pop("id")
        await self._docs.put(PENDING_INSIGHTS_COLLECTION, insight.id, updated)
        return f"{insight.text} {insight.caveat}".strip()

    async def dismiss_insight(self, insight_id: str, user_id: str) -> None:
        doc = await self._docs.get(PENDING_INSIGHTS_COLLECTION, insight_id)
        if doc is None or doc.get("user_id") != user_id:
            raise ProjectNotFound(insight_id)
        doc["status"] = "dismissed"
        await self._docs.put(PENDING_INSIGHTS_COLLECTION, insight_id, doc)

    # ── internals ────────────────────────────────────────────────────────

    def _register_type_tools(self, type_id: str, blueprint: Mapping[str, Any]) -> None:
        if self._registry is None:
            return
        for action in blueprint.get("actions", []):
            spec = ToolSpec(
                id=f"{type_id}.{action['id']}",
                description=f"{action['id']} on a {type_id} project",
                input_schema=dict(action.get("schema", {})),
                type=action.get("type", "action"),
                latency_class=action.get("latency_class", "fast"),
                requires_confirmation=bool(action.get("requires_confirmation", True)),
                interruptible=False,
                scope=f"project:{type_id}",
            )
            self._registry.register(spec, self._action_handler(action["id"]))

    def _action_handler(self, action_id: str) -> Any:
        async def handle(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
            if action_id != "log_entry":
                raise ValueError(f"unknown project action '{action_id}'")
            project_id = str(args.pop("project_id", None) or context.project_id or "")
            entry = await self.log_entry(project_id, context.user_id, args)
            insight = await self.run_insight(project_id, context.user_id)
            return {
                "logged": entry.data,
                "entry_id": entry.id,
                "pending_insight": insight.id if insight else None,
            }

        return handle

    async def _compose_insight(
        self, user_id: str, project: Project, state: ProjectState
    ) -> str | None:
        if self._llm is None:
            # Deterministic factual fallback so the mechanism works without an LLM.
            return f"Quick factual note on {project.name}: metrics are {state.metrics}."
        messages = [
            {"role": "system", "content": _INSIGHT_INSTRUCTIONS},
            {"role": "user", "content": f"Project {project.name} metrics: {state.metrics}"},
        ]
        try:
            result = await self._llm.complete(user_id, messages, "simple")
        except LLMUnavailable:
            logger.warning("insight composition unavailable; skipping")
            return None
        return result.text.strip() or None

    async def _project(self, project_id: str, user_id: str) -> Project:
        doc = await self._docs.get(PROJECTS_COLLECTION, project_id)
        if doc is None or doc.get("user_id") != user_id:
            raise ProjectNotFound(project_id)
        return Project.model_validate(
            {"id": doc["_id"], **{k: v for k, v in doc.items() if k != "_id"}}
        )

    async def _entries(self, project_id: str, user_id: str) -> list[LedgerEntry]:
        docs = await self._docs.find(
            LEDGER_ENTRIES_COLLECTION,
            {"project_id": project_id, "user_id": user_id},
            limit=1000,
        )
        entries = [
            LedgerEntry.model_validate(
                {"id": d["_id"], **{k: v for k, v in d.items() if k != "_id"}}
            )
            for d in docs
        ]
        entries.sort(key=lambda e: e.timestamp)
        return entries

    async def _blueprint(self, type_id: str) -> dict[str, Any]:
        doc = await self._docs.get(PROJECT_TYPES_COLLECTION, type_id)
        if doc is None:
            raise ProjectNotFound(f"unknown project type '{type_id}'")
        return doc


def _finance_metrics(entries: list[LedgerEntry]) -> dict[str, Any]:
    """Average-cost accounting from the append-only ledger (rule 2)."""
    positions: dict[str, dict[str, float]] = {}
    realized_pnl = 0.0
    net_invested = 0.0
    for entry in entries:
        data = entry.data
        ticker = str(data.get("ticker", "?")).upper()
        side = str(data.get("side", "")).lower()
        qty = float(data.get("qty", 0))
        price = float(data.get("price", 0))
        position = positions.setdefault(ticker, {"qty": 0.0, "avg_cost": 0.0})
        if side == "buy":
            total_cost = position["qty"] * position["avg_cost"] + qty * price
            position["qty"] += qty
            position["avg_cost"] = total_cost / position["qty"] if position["qty"] else 0.0
            net_invested += qty * price
        elif side == "sell":
            realized_pnl += (price - position["avg_cost"]) * qty
            position["qty"] -= qty
            net_invested -= qty * price
            if position["qty"] <= 0:
                position["qty"] = max(position["qty"], 0.0)
    return {
        "positions": {
            t: {"qty": p["qty"], "avg_cost": round(p["avg_cost"], 4)}
            for t, p in positions.items()
            if p["qty"] > 0
        },
        "realized_pnl": round(realized_pnl, 4),
        "net_invested": round(net_invested, 4),
    }


def _insight_from_doc(doc: Mapping[str, Any]) -> Insight:
    return Insight.model_validate(
        {"id": doc["_id"], **{k: v for k, v in doc.items() if k != "_id"}}
    )
