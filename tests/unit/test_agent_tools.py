from __future__ import annotations

from unittest.mock import Mock

import pytest

from quant_system.agents.protocol import FORBIDDEN_TOOL_NAMES, ToolCall, ToolRegistry
from quant_system.agents.tools import (
    ArtifactTool,
    BacktestTool,
    DataCatalogTool,
    ExperimentTool,
    PITQueryTool,
    RegistryTool,
    ReportTool,
)


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


@pytest.mark.parametrize(
    "tool_class,method,arguments",
    [
        (DataCatalogTool, "find_by_type", {}),
        (PITQueryTool, "get_bars", {"start": "2025-01-02", "end": "2025-01-03", "as_of_date": "2025-01-03"}),
        (
            BacktestTool,
            "run",
            {
                "request": {
                    "strategy_id": "momentum_topk",
                    "dataset_snapshot_id": "snapshot",
                    "start_date": "2025-01-02",
                    "end_date": "2025-01-03",
                    "initial_cash": "1000000",
                }
            },
        ),
        (ArtifactTool, "get", {"artifact_id": "artifact"}),
        (RegistryTool, "lineage", {"artifact_id": "artifact"}),
        (ExperimentTool, "list_all", {}),
        (ExperimentTool, "create", {"action": "create", "name": "experiment"}),
        (ReportTool, "register", {}),
    ],
)
def test_service_failures_are_returned_as_tool_errors(tool_class, method, arguments):
    service = Mock()
    getattr(service, method).side_effect = RuntimeError("service unavailable")
    call = ToolCall(trace_id="trace", agent_id="agent", arguments=arguments)
    result = tool_class(service).invoke(call)
    assert result.call_id == call.call_id
    assert result.status == "ERROR"
    assert result.error == "RuntimeError: service unavailable"
    assert result.artifact_ids == []
    getattr(service, method).assert_called_once()


def test_tool_error_wrapper_does_not_swallow_interrupts():
    service = Mock()
    service.find_by_type.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        DataCatalogTool(service).invoke(ToolCall(trace_id="trace", agent_id="agent"))
