from __future__ import annotations

import pytest

from quant_system.agents.protocol import FORBIDDEN_TOOL_NAMES, ToolCall, ToolRegistry
from quant_system.agents.tools import DataCatalogTool


def test_forbidden_tool_names_cannot_be_registered():
    registry = ToolRegistry()

    class EvilTool:
        name = "send_live_order"

        def invoke(self, call):  # pragma: no cover
            raise AssertionError("must never run")

    for name in FORBIDDEN_TOOL_NAMES:
        EvilTool.name = name
        with pytest.raises(PermissionError):
            registry.register(EvilTool())


def test_tool_result_carries_artifact_references(registry):
    tool = DataCatalogTool(registry)
    call = ToolCall(trace_id="tr1", agent_id="agent1")
    result = tool.invoke(call)
    assert result.status == "OK"
    assert result.call_id == call.call_id
    assert result.artifact_ids == []
    assert result.summary["n_snapshots"] == 0
