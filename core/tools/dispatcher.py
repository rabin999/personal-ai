"""Tool Dispatcher (spec §13): the agentic loop and class-based dispatch.

- readonly/fast → run inline, result feeds back into the same turn
- background/slow → enqueue (§14), conversation continues
- action → confirmation first; once confirmed, executes shielded so a
  barge-in can never cancel a write mid-flight (rule 6)
- variable → runs with a time budget; overruns get promoted to the queue

Every dispatch logs a Cost Ledger entry; tools that spend real money (e.g.
web search) additionally log their own provider costs internally.
"""

import asyncio
import json
import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from core.cost import CostEntry, CostLedger, CostMetadata
from core.observability.logger import StructuredLogger
from core.reasoning.prompt_assembly import AssembledPrompt
from core.steps import StepStatus, run_step
from core.tools.registry import ToolContext, ToolRegistry, ToolSpec
from core.tools.results import ToolResultStore
from ports.llm import LLM, LLMUnavailable
from ports.queue import QueuedTask, TaskQueue

logger = logging.getLogger(__name__)

# Budget for "variable" latency tools before promotion to the queue (rule 2).
VARIABLE_BUDGET_S = 0.8

TOOL_TASK_TYPE = "tool"

MAX_LOOP_STEPS = 4

_LOOP_INSTRUCTIONS = """
You may use tools. Respond ONLY with JSON, one of:
{"action": "tool", "tool_id": "<id>", "args": {<per the tool's input schema>}}
{"action": "final", "text": "<your reply to the user>"}
Available tools:
"""


class ToolCall(BaseModel):
    tool_id: str
    args: dict[str, Any] = {}


class ToolResult(BaseModel):
    tool_id: str
    output: dict[str, Any]
    elapsed_ms: float
    # Unified step envelope (Item 5): a broken tool becomes a clean failure result
    # (status + error, empty output) instead of a raised exception — the turn still
    # completes and the failure is visible in the trace.
    status: StepStatus = "success"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("success", "skipped", "not_available")


class QueuedHandle(BaseModel):
    tool_id: str
    task_id: str


class ConfirmRequest(BaseModel):
    tool_id: str
    args: dict[str, Any]
    prompt_text: str


class LoopOutcome(BaseModel):
    kind: Literal["final", "confirm"]
    text: str | None = None
    confirm: ConfirmRequest | None = None
    tool_results: list[ToolResult] = []
    queued: list[QueuedHandle] = []


class ToolDispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        queue: TaskQueue,
        ledger: CostLedger | None = None,
        variable_budget_s: float = VARIABLE_BUDGET_S,
        results: ToolResultStore | None = None,
        logs: StructuredLogger | None = None,
    ) -> None:
        self._registry = registry
        self._queue = queue
        self._ledger = ledger
        self._variable_budget_s = variable_budget_s
        self._results = results
        self._logs = logs

    async def dispatch(
        self, call: ToolCall, context: ToolContext, *, confirmed: bool = False
    ) -> ToolResult | QueuedHandle | ConfirmRequest:
        spec, handler = self._registry.get(call.tool_id)

        if spec.type == "action" and spec.requires_confirmation and not confirmed:
            return ConfirmRequest(
                tool_id=spec.id,
                args=call.args,
                prompt_text=f"Should I go ahead and run {spec.id}?",
            )

        if spec.type == "background" or spec.latency_class == "slow":
            return await self._enqueue(call, context)

        started = time.perf_counter()
        try:
            if spec.type == "action":
                # Rule 6: writes are never cancelled mid-execution; a barge-in
                # is handled after the write completes (§24).
                output = await asyncio.shield(asyncio.ensure_future(handler(call.args, context)))
            elif spec.latency_class == "variable":
                try:
                    output = await asyncio.wait_for(
                        handler(call.args, context), timeout=self._variable_budget_s
                    )
                except TimeoutError:
                    logger.info("variable tool %s overran budget; promoting to queue", spec.id)
                    return await self._enqueue(call, context)
            else:
                output = await handler(call.args, context)
        except asyncio.CancelledError:
            raise  # barge-in / shutdown — must propagate (§24)
        except Exception as exc:  # Item 5: broken tool → clean failure envelope
            return self._tool_failure(spec, call, context, started, exc, spec.type)

        elapsed_ms = (time.perf_counter() - started) * 1000
        self._log(spec, context)
        self._tool_span(spec.id, spec.type, call.args, elapsed_ms, output, "success", None)
        await self._persist_result(spec.id, call.args, output, context)
        return ToolResult(tool_id=spec.id, output=output, elapsed_ms=elapsed_ms)

    def _tool_span(
        self,
        tool_id: str,
        tool_type: str,
        args: dict[str, Any],
        elapsed_ms: float,
        output: dict[str, Any] | None,
        status: StepStatus,
        error: str | None,
    ) -> None:
        """Tool-call span in the per-turn trace with the unified envelope fields."""
        if self._logs is None:
            return
        self._logs.log(
            "info" if status == "success" else "warn",
            "tool.call",
            stage="tool",
            tool=tool_id,
            tool_type=tool_type,
            args=args,
            status=status,
            ok=status in ("success", "skipped", "not_available"),
            error=error,
            latency_ms=round(elapsed_ms, 1),
            result=json.dumps(output)[:300] if output else "",
        )

    def _tool_failure(
        self,
        spec: ToolSpec,
        call: "ToolCall",
        context: ToolContext,
        started: float,
        exc: Exception,
        tool_type: str,
    ) -> ToolResult:
        elapsed_ms = (time.perf_counter() - started) * 1000
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("tool %s failed: %s", spec.id, error)
        self._tool_span(
            spec.id, f"{tool_type}:failed", call.args, elapsed_ms, None, "failure", error
        )
        return ToolResult(
            tool_id=spec.id, output={}, elapsed_ms=elapsed_ms, status="failure", error=error
        )

    def tools_for(self, context: ToolContext) -> list[ToolSpec]:
        """Tools available in this context (core + the referenced project's)."""
        return self._registry.tools_for_context(context.project_type)

    async def run_inline(
        self, call: ToolCall, context: ToolContext, *, timeout_s: float = 8.0
    ) -> ToolResult:
        """Execute a tool synchronously in-turn, ignoring its latency class.

        Used by the response loop's capability backstop (brief §8.8/§8.11) to
        answer a live-info question in the SAME turn instead of the background/
        waiter path — bounded by ``timeout_s`` so one slow provider can't stall
        the reply. Still logs the tool span, cost, and persists the result.
        """
        spec, handler = self._registry.get(call.tool_id)
        started = time.perf_counter()
        # Item 5: a broken/slow inline tool becomes a clean failure/timeout
        # envelope (not a raised exception), so the response loop's backstop can
        # fall through to the model's own words instead of the turn crashing.
        result, output = await run_step(
            f"tool:{spec.id}", handler(call.args, context), timeout_s=timeout_s
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        if not result.ok or output is None:
            self._tool_span(
                spec.id,
                f"{spec.type}:inline:{result.status}",
                call.args,
                elapsed_ms,
                None,
                result.status,
                result.error,
            )
            return ToolResult(
                tool_id=spec.id,
                output={},
                elapsed_ms=elapsed_ms,
                status=result.status,
                error=result.error,
            )
        self._log(spec, context)
        self._tool_span(
            spec.id, f"{spec.type}:inline", call.args, elapsed_ms, output, "success", None
        )
        await self._persist_result(spec.id, call.args, output, context)
        return ToolResult(tool_id=spec.id, output=output, elapsed_ms=elapsed_ms)

    async def loop(self, prompt: AssembledPrompt, llm: LLM, context: ToolContext) -> LoopOutcome:
        """ReAct-style loop: tools until a direct answer (or a confirmation gate)."""
        tools = self._registry.tools_for_context(context.project_type)
        messages: list[dict[str, str]] = [
            *prompt.messages,
            {"role": "system", "content": _render_tool_instructions(tools)},
        ]
        results: list[ToolResult] = []
        queued: list[QueuedHandle] = []

        for _ in range(MAX_LOOP_STEPS):
            step = await self._loop_step(prompt, llm, messages)
            if step is None:
                break
            if step.action == "final":
                return LoopOutcome(
                    kind="final", text=step.text, tool_results=results, queued=queued
                )
            call = ToolCall(tool_id=step.tool_id or "", args=step.args)
            try:
                dispatched = await self.dispatch(call, context)
            except KeyError:
                messages.append(
                    {"role": "system", "content": f"Tool {call.tool_id} does not exist."}
                )
                continue
            if isinstance(dispatched, ConfirmRequest):
                return LoopOutcome(
                    kind="confirm", confirm=dispatched, tool_results=results, queued=queued
                )
            if isinstance(dispatched, QueuedHandle):
                queued.append(dispatched)
                messages.append(
                    {
                        "role": "system",
                        "content": f"Tool {call.tool_id} is running in the background; "
                        "its result will arrive later. Answer with what you have.",
                    }
                )
                continue
            results.append(dispatched)
            messages.append(
                {
                    "role": "system",
                    "content": f"Tool {call.tool_id} returned: "
                    f"{json.dumps(dispatched.output)[:2000]}",
                }
            )

        return LoopOutcome(
            kind="final",
            text="Sorry — I couldn't finish that; want me to try again?",
            tool_results=results,
            queued=queued,
        )

    def task_handler(self) -> Any:
        """§14 worker handler executing queue-promoted tool calls."""

        async def handle(task: QueuedTask) -> dict[str, Any]:
            call = ToolCall(
                tool_id=str(task.params["tool_id"]), args=dict(task.params.get("args", {}))
            )
            context = ToolContext(
                user_id=task.user_id,
                session_id=task.session_id,
                project_id=task.params.get("project_id"),
                project_type=task.params.get("project_type"),
            )
            spec, handler = self._registry.get(call.tool_id)
            output = await handler(call.args, context)
            self._log(spec, context)
            await self._persist_result(spec.id, call.args, output, context)
            return output

        return handle

    async def _enqueue(self, call: ToolCall, context: ToolContext) -> QueuedHandle:
        task_id = await self._queue.enqueue(
            session_id=context.session_id,
            user_id=context.user_id,
            type=TOOL_TASK_TYPE,
            params={
                "tool_id": call.tool_id,
                "args": call.args,
                "project_id": context.project_id,
                "project_type": context.project_type,
            },
        )
        return QueuedHandle(tool_id=call.tool_id, task_id=task_id)

    async def _loop_step(
        self, prompt: AssembledPrompt, llm: LLM, messages: list[dict[str, str]]
    ) -> "_LoopStep | None":
        for _ in range(2):  # validate; retry once (§0.5)
            try:
                result = await llm.complete(
                    prompt.user_id,
                    messages,
                    prompt.complexity_hint,
                    response_format={"type": "json_object"},
                    session_id=prompt.session_id,
                    purpose="tool_react",
                )
                return _LoopStep.model_validate_json(result.text)
            except (LLMUnavailable, ValidationError, ValueError):
                continue
        return None

    async def _persist_result(
        self, tool_id: str, args: dict[str, Any], output: dict[str, Any], context: ToolContext
    ) -> None:
        """Store the result so 'what was that news?' resolves later (§5.2)."""
        if self._results is None:
            return
        await self._results.record(
            user_id=context.user_id,
            session_id=context.session_id,
            tool_id=tool_id,
            args=args,
            output=output,
        )

    def _log(self, spec: ToolSpec, context: ToolContext) -> None:
        if self._ledger is None:
            return
        self._ledger.log(
            CostEntry(
                user_id=context.user_id,
                component="tool",
                provider="internal",
                units={"calls": 1},
                cost_usd=0.0,  # paid tools log their own provider costs
                metadata=CostMetadata(
                    session_id=context.session_id,
                    project_id=context.project_id,
                    task_id=spec.id,
                ),
            )
        )


class _LoopStep(BaseModel):
    action: Literal["tool", "final"]
    tool_id: str | None = None
    args: dict[str, Any] = {}
    text: str | None = None


def _render_tool_instructions(tools: list[ToolSpec]) -> str:
    lines = [
        f"- {t.id}: {t.description} (input schema: {json.dumps(t.input_schema)})" for t in tools
    ]
    return _LOOP_INSTRUCTIONS + ("\n".join(lines) if lines else "(none available)")
