"""Concrete research tools. Thin wrappers over domain services; results are
artifact references + small summaries, never giant dataframes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from functools import wraps
from typing import TypeVar

from quant_system.agents.protocol import ToolCall, ToolResult
from quant_system.artifacts.lineage import ArtifactLineageService
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.types import deployment_proposal_artifact
from quant_system.backtest.request import BacktestEngine, BacktestRequest
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.domain.enums import ArtifactType
from quant_system.domain.types import bar_available_at
from quant_system.research.experiments import ExperimentService

ToolInstance = TypeVar("ToolInstance")


def _safe_invoke(
    invoke: Callable[[ToolInstance, ToolCall], ToolResult],
) -> Callable[[ToolInstance, ToolCall], ToolResult]:
    @wraps(invoke)
    def wrapped(self: ToolInstance, call: ToolCall) -> ToolResult:
        try:
            return invoke(self, call)
        except Exception as exc:
            return ToolResult(
                call_id=call.call_id,
                status="ERROR",
                error=f"{type(exc).__name__}: {exc}",
            )

    return wrapped


class DataCatalogTool:
    name = "data_catalog"

    def __init__(self, registry: ArtifactRegistry) -> None:
        self._registry = registry

    @_safe_invoke
    def invoke(self, call: ToolCall) -> ToolResult:
        snapshots = self._registry.find_by_type(ArtifactType.DATA_SNAPSHOT)
        return ToolResult(
            call_id=call.call_id,
            status="OK",
            artifact_ids=[snapshot.artifact_id for snapshot in snapshots],
            summary={"n_snapshots": len(snapshots)},
        )


class PITQueryTool:
    name = "pit_query"

    def __init__(self, provider: LocalParquetProvider) -> None:
        self._provider = provider

    @_safe_invoke
    def invoke(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        start = date.fromisoformat(args["start"])
        end = date.fromisoformat(args["end"])
        as_of = bar_available_at(date.fromisoformat(args["as_of_date"]))
        bars = self._provider.get_bars(args.get("symbols"), start, end, as_of=as_of)
        return ToolResult(
            call_id=call.call_id,
            status="OK",
            summary={"n_bars": len(bars), "symbols": sorted({bar.symbol for bar in bars})[:50]},
        )


class BacktestTool:
    name = "backtest"

    def __init__(self, engine: BacktestEngine) -> None:
        self._engine = engine

    @_safe_invoke
    def invoke(self, call: ToolCall) -> ToolResult:
        request = BacktestRequest(**call.arguments["request"])
        if call.experiment_id and request.experiment_id != call.experiment_id:
            request = request.model_copy(update={"experiment_id": call.experiment_id})
        result = self._engine.run(request)
        return ToolResult(
            call_id=call.call_id,
            status="OK",
            artifact_ids=[result.result_artifact_id, result.spec_artifact_id],
            metrics={**result.performance_metrics, **result.execution_metrics},
        )


class ArtifactTool:
    name = "artifact"

    def __init__(self, registry: ArtifactRegistry) -> None:
        self._registry = registry

    @_safe_invoke
    def invoke(self, call: ToolCall) -> ToolResult:
        artifact = self._registry.get(call.arguments["artifact_id"])
        return ToolResult(
            call_id=call.call_id,
            status="OK",
            artifact_ids=[artifact.artifact_id],
            summary={
                "type": artifact.artifact_type.value,
                "uri": artifact.uri,
                "metadata_keys": sorted(artifact.metadata),
            },
        )


class RegistryTool:
    name = "registry_lineage"

    def __init__(self, lineage: ArtifactLineageService) -> None:
        self._lineage = lineage

    @_safe_invoke
    def invoke(self, call: ToolCall) -> ToolResult:
        graph = self._lineage.lineage(call.arguments["artifact_id"])
        return ToolResult(
            call_id=call.call_id,
            status="OK",
            summary={
                "ancestors": [artifact.artifact_id for artifact in graph["ancestors"]],
                "descendants": [artifact.artifact_id for artifact in graph["descendants"]],
            },
        )


class ExperimentTool:
    name = "experiment"

    def __init__(self, service: ExperimentService) -> None:
        self._service = service

    @_safe_invoke
    def invoke(self, call: ToolCall) -> ToolResult:
        action = call.arguments.get("action", "list")
        if action == "create":
            experiment = self._service.create(call.arguments["name"], call.arguments.get("description", ""))
            return ToolResult(
                call_id=call.call_id,
                status="OK",
                summary={"experiment_id": experiment.experiment_id, "state": experiment.state.value},
            )
        experiments = self._service.list_all()
        return ToolResult(
            call_id=call.call_id,
            status="OK",
            summary={
                "experiments": [
                    {"id": experiment.experiment_id, "name": experiment.name, "state": experiment.state.value}
                    for experiment in experiments
                ]
            },
        )


class ReportTool:
    """Agents may create a DeploymentProposal artifact. Activation requires a
    human-operated deployment flow that does not exist as an agent tool."""

    name = "deployment_proposal"

    def __init__(self, registry: ArtifactRegistry) -> None:
        self._registry = registry

    @_safe_invoke
    def invoke(self, call: ToolCall) -> ToolResult:
        artifact = deployment_proposal_artifact(
            producer=f"agent:{call.agent_id}",
            experiment_id=call.experiment_id,
            parents=tuple(call.arguments.get("evidence_artifact_ids", [])),
            metadata={"proposal": call.arguments.get("proposal", {}), "trace_id": call.trace_id},
        )
        self._registry.register(artifact)
        return ToolResult(call_id=call.call_id, status="OK", artifact_ids=[artifact.artifact_id])


class CodeSandboxTool:
    """Interface placeholder: executing agent code requires a sandbox which is
    intentionally not implemented in V1."""

    name = "code_sandbox"

    def invoke(self, call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.call_id, status="ERROR", error="code sandbox not implemented in V1")
