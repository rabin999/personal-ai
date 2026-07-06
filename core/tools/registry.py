"""Tool registry (spec §13): MCP-shaped, context-scoped.

Each tool is a spec (name, description, input schema — the MCP triplet) plus
execution class metadata and a handler. External MCP servers can slot in
later by registering their tools here (backlog); nothing else changes.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

ToolType = Literal["readonly", "background", "action"]
LatencyClass = Literal["fast", "slow", "variable"]

CORE_SCOPE = "core"


class ToolSpec(BaseModel):
    id: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    type: ToolType = "readonly"
    latency_class: LatencyClass = "fast"
    requires_confirmation: bool = False
    interruptible: bool = True
    scope: str = CORE_SCOPE  # "core" or "project:<project_type_id>"


class ToolContext(BaseModel):
    user_id: str
    session_id: str
    project_id: str | None = None
    project_type: str | None = None


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


class UnknownTool(KeyError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self._specs[spec.id] = spec
        self._handlers[spec.id] = handler

    def get(self, tool_id: str) -> tuple[ToolSpec, ToolHandler]:
        if tool_id not in self._specs:
            raise UnknownTool(tool_id)
        return self._specs[tool_id], self._handlers[tool_id]

    def tools_for_context(self, project_type: str | None = None) -> list[ToolSpec]:
        """Core tools + the referenced project type's tools — never the full registry (rule 3)."""
        allowed_scopes = {CORE_SCOPE}
        if project_type:
            allowed_scopes.add(f"project:{project_type}")
        return [s for s in self._specs.values() if s.scope in allowed_scopes]
