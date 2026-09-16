"""Agent Tool Protocol.

Agents do research; they never touch live execution. There are deliberately
NO tools named send_live_order / change_risk_limit / transfer_funds, and the
tool registry refuses to register anything with those names.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from quant_system.domain.types import new_id

FORBIDDEN_TOOL_NAMES = frozenset({"send_live_order", "change_risk_limit", "transfer_funds"})


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: str = Field(default_factory=lambda: new_id("call_"))
    trace_id: str
    agent_id: str
    experiment_id: str | None = None
    timeout_seconds: float = 300.0
    resource_budget: dict[str, Any] = {}
    arguments: dict[str, Any] = {}


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: str
    status: str  # OK | ERROR | TIMEOUT | BUDGET_EXCEEDED
    artifact_ids: list[str] = []
    metrics: dict[str, Any] = {}
    logs_uri: str = ""
    error: str | None = None
    # Small summaries only; large data must be referenced via artifact_ids.
    summary: dict[str, Any] = {}


class Tool(Protocol):
    name: str

    def invoke(self, call: ToolCall) -> ToolResult: ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in FORBIDDEN_TOOL_NAMES:
            raise PermissionError(f"tool name '{tool.name}' is forbidden for agents")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name in FORBIDDEN_TOOL_NAMES:
            raise PermissionError(f"tool '{name}' does not exist and never will for agents")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)
