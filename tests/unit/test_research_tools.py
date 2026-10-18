from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from quant_system.domain.exceptions import QuantSystemError
from quant_system.research.runtime.models import TaskDescriptor
from quant_system.research.runtime.tools import ResearchTool, ToolExecutor


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


def test_only_allowlisted_typed_tools_execute_and_emit_audit():
    events = []
    checks = []
    ctx = SimpleNamespace(
        check=lambda: checks.append(True), advance=lambda stage, **data: events.append((stage, data))
    )
    executor = ToolExecutor()
    executor.register(
        ResearchTool("documents.count", ToolRequest, lambda ctx, request: request.value + 1, "documents.read")
    )
    descriptor = TaskDescriptor(
        task_type="documents.inspect",
        module_id="documents",
        title="Inspect",
        tools={"reader": ["documents.count"]},
        capabilities=["documents.read"],
    )
    assert executor.invoke(ctx, "reader", "documents.count", {"value": 2}, descriptor) == 3
    assert [stage for stage, _ in events] == ["tool_started", "tool_completed"]
    assert len(checks) >= 3
    assert not hasattr(executor, "get")
    for role, name, payload, desc in (
        ("other", "documents.count", {"value": 2}, descriptor),
        ("reader", "unregistered", {"value": 2}, descriptor),
        ("reader", "documents.count", {"value": 2, "url": "https://example.invalid"}, descriptor),
        ("reader", "documents.count", {"value": "bad"}, descriptor),
        ("reader", "documents.count", {"value": 2}, descriptor.model_copy(update={"capabilities": []})),
    ):
        with pytest.raises(QuantSystemError):
            executor.invoke(ctx, role, name, payload, desc)


@pytest.mark.parametrize(
    "name", ["send_live_order", "broker", "shell", "tools.exec", "import", "python", "script"]
)
def test_forbidden_tools_never_register(name):
    with pytest.raises(QuantSystemError, match="forbidden"):
        ToolExecutor().register(ResearchTool(name, ToolRequest, lambda *_: None, "documents.read"))


def test_tool_deadline_is_checked_after_cooperative_callable_returns():
    done = []

    def check():
        if done:
            raise QuantSystemError("research deadline exceeded")

    ctx = SimpleNamespace(check=check, advance=lambda *_args, **_kwargs: None)
    executor = ToolExecutor()
    executor.register(ResearchTool("documents.slow", ToolRequest, lambda *_: done.append(True), "read"))
    descriptor = TaskDescriptor(
        task_type="documents.inspect",
        module_id="documents",
        title="Inspect",
        tools={"reader": ["documents.slow"]},
        capabilities=["read"],
    )
    with pytest.raises(QuantSystemError, match="deadline"):
        executor.invoke(ctx, "reader", "documents.slow", {"value": 2}, descriptor)
    assert done


def test_nested_tool_payload_cannot_smuggle_unknown_execution_fields():
    class Options(BaseModel):
        value: int

    class Request(BaseModel):
        options: Options

    executor = ToolExecutor()
    invoked = []
    executor.register(ResearchTool("documents.nested", Request, lambda *_: invoked.append(True), "read"))
    descriptor = TaskDescriptor(
        task_type="documents.inspect",
        module_id="documents",
        title="Inspect",
        tools={"reader": ["documents.nested"]},
        capabilities=["read"],
    )
    ctx = SimpleNamespace(check=lambda: None, advance=lambda *_args, **_kwargs: None)
    with pytest.raises(QuantSystemError, match="nested"):
        executor.invoke(
            ctx,
            "reader",
            "documents.nested",
            {"options": {"value": 1, "script": "not executable"}},
            descriptor,
        )
    assert not invoked
