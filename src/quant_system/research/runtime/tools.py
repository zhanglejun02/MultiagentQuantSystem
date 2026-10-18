from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from quant_system.domain.exceptions import QuantSystemError
from quant_system.research.runtime.models import TaskDescriptor
from quant_system.research.runtime.planning import validate_input_fields

if TYPE_CHECKING:
    from quant_system.research.runtime.engine import RunContext


_FORBIDDEN = {
    "send_live_order",
    "place_order",
    "submit_order",
    "cancel_order",
    "trade",
    "broker",
    "shell",
    "exec",
    "eval",
    "import",
    "python",
    "script",
    "subprocess",
}


@dataclass(frozen=True)
class ResearchTool:
    tool_id: str
    request_type: type[BaseModel]
    invoke: Callable[[RunContext, BaseModel], Any]
    capability: str


class ToolExecutor:
    def __init__(self) -> None:
        self._tools: dict[str, ResearchTool] = {}

    def register(self, tool: ResearchTool) -> None:
        pieces = tool.tool_id.casefold().split(".")
        if (
            re.fullmatch(r"[a-z][a-z0-9_.-]*", tool.tool_id) is None
            or any(piece in _FORBIDDEN for piece in pieces)
            or not tool.capability
            or tool.capability.casefold() in _FORBIDDEN
        ):
            raise QuantSystemError("forbidden research tool name or capability")
        if tool.tool_id in self._tools:
            raise QuantSystemError(f"research tool already registered: {tool.tool_id}")
        if not isinstance(tool.request_type, type) or not issubclass(tool.request_type, BaseModel):
            raise QuantSystemError("research tool requires a typed request model")
        self._tools[tool.tool_id] = tool

    def invoke(
        self,
        ctx: RunContext,
        role: str,
        tool_id: str,
        payload: dict[str, Any],
        taskdesc: TaskDescriptor,
    ) -> Any:
        ctx.check()
        try:
            tool = self._tools.get(tool_id)
            if tool is None or tool_id not in taskdesc.tools.get(role, []):
                raise QuantSystemError("research tool is not allowed for this task and role")
            if tool.capability not in taskdesc.capabilities:
                raise QuantSystemError("research tool capability is not allowed for this task")
            if not isinstance(payload, dict) or set(payload) - tool.request_type.model_fields.keys():
                raise QuantSystemError("unknown research tool request fields")
            validate_input_fields(payload, tool.request_type.model_json_schema())
            request = tool.request_type.model_validate(payload)
        except (QuantSystemError, ValidationError) as exc:
            ctx.advance("tool_denied", event_type="tool_denied", role=role, tool_id=tool_id)
            raise QuantSystemError(str(exc)) from exc
        ctx.advance(
            "tool_started", event_type="tool_started", role=role, tool_id=tool_id, capability=tool.capability
        )
        ctx.check()
        try:
            result = tool.invoke(ctx, request)
            ctx.check()
        except BaseException:
            try:
                ctx.advance("tool_failed", event_type="tool_failed", role=role, tool_id=tool_id)
            except QuantSystemError:
                pass
            raise
        ctx.advance("tool_completed", event_type="tool_completed", role=role, tool_id=tool_id)
        return result
