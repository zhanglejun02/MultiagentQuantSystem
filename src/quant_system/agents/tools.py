"""Concrete research tools. Thin wrappers over domain services; results are
artifact references + small summaries, never giant dataframes."""

from __future__ import annotations

from datetime import date

from quant_system.agents.protocol import ToolCall, ToolResult
from quant_system.artifacts.lineage import ArtifactLineageService
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.types import deployment_proposal_artifact
from quant_system.backtest.request import BacktestEngine, BacktestRequest
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.domain.enums import ArtifactType
from quant_system.domain.types import bar_available_at
from quant_system.research.experiments import ExperimentService


def _error(call: ToolCall, exc: Exception) -> ToolResult:
    return ToolResult(call_id=call.call_id, status="ERROR",
                      error=f"{type(exc).__name__}: {exc}")


class DataCatalogTool:
    name = "data_catalog"

    def __init__(self, registry: ArtifactRegistry) -> None:
        self._registry = registry

    def invoke(self, call: ToolCall) -> ToolResult:
        try:
            snapshots = self._registry.find_by_type(ArtifactType.DATA_SNAPSHOT)
            return ToolResult(
                call_id=call.call_id, status="OK",
                artifact_ids=[s.artifact_id for s in snapshots],
                summary={"n_snapshots": len(snapshots)},
            )
        except Exception as exc:  # noqa: BLE001
            return _error(call, exc)


class PITQueryTool:
    name = "pit_query"

    def __init__(self, provider: LocalParquetProvider) -> None:
        self._provider = provider

    def invoke(self, call: ToolCall) -> ToolResult:
        try:
            args = call.arguments
            start = date.fromisoformat(args["start"])
            end = date.fromisoformat(args["end"])
            as_of = bar_available_at(date.fromisoformat(args["as_of_date"]))
            bars = self._provider.get_bars(args.get("symbols"), start, end, as_of=as_of)
            return ToolResult(
                call_id=call.call_id, status="OK",
                summary={"n_bars": len(bars),
                         "symbols": sorted({b.symbol for b in bars})[:50]},
            )
        except Exception as exc:  # noqa: BLE001
            return _error(call, exc)


class BacktestTool:
    name = "backtest"

    def __init__(self, engine: BacktestEngine) -> None:
        self._engine = engine

    def invoke(self, call: ToolCall) -> ToolResult:
        try:
            request = BacktestRequest(**call.arguments["request"])
            if call.experiment_id and request.experiment_id != call.experiment_id:
                request = request.model_copy(update={"experiment_id": call.experiment_id})
            result = self._engine.run(request)
            return ToolResult(
                call_id=call.call_id, status="OK",
                artifact_ids=[result.result_artifact_id, result.spec_artifact_id],
                metrics={**result.performance_metrics, **result.execution_metrics},
            )
        except Exception as exc:  # noqa: BLE001
            return _error(call, exc)


class ArtifactTool:
    name = "artifact"

    def __init__(self, registry: ArtifactRegistry) -> None:
        self._registry = registry

    def invoke(self, call: ToolCall) -> ToolResult:
        try:
            env = self._registry.get(call.arguments["artifact_id"])
            return ToolResult(
                call_id=call.call_id, status="OK", artifact_ids=[env.artifact_id],
                summary={"type": env.artifact_type.value, "uri": env.uri,
                         "metadata_keys": sorted(env.metadata)},
            )
        except Exception as exc:  # noqa: BLE001
            return _error(call, exc)


class RegistryTool:
    name = "registry_lineage"

    def __init__(self, lineage: ArtifactLineageService) -> None:
        self._lineage = lineage

    def invoke(self, call: ToolCall) -> ToolResult:
        try:
            graph = self._lineage.lineage(call.arguments["artifact_id"])
            return ToolResult(
                call_id=call.call_id, status="OK",
                summary={
                    "ancestors": [a.artifact_id for a in graph["ancestors"]],
                    "descendants": [d.artifact_id for d in graph["descendants"]],
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _error(call, exc)


class ExperimentTool:
    name = "experiment"

    def __init__(self, service: ExperimentService) -> None:
        self._service = service

    def invoke(self, call: ToolCall) -> ToolResult:
        try:
            action = call.arguments.get("action", "list")
            if action == "create":
                exp = self._service.create(call.arguments["name"],
                                           call.arguments.get("description", ""))
                return ToolResult(call_id=call.call_id, status="OK",
                                  summary={"experiment_id": exp.experiment_id,
                                           "state": exp.state.value})
            exps = self._service.list_all()
            return ToolResult(
                call_id=call.call_id, status="OK",
                summary={"experiments": [
                    {"id": e.experiment_id, "name": e.name, "state": e.state.value}
                    for e in exps
                ]},
            )
        except Exception as exc:  # noqa: BLE001
            return _error(call, exc)


class ReportTool:
    """Agents may create a DeploymentProposal artifact. Activation requires a
    human-operated deployment flow that does not exist as an agent tool."""

    name = "deployment_proposal"

    def __init__(self, registry: ArtifactRegistry) -> None:
        self._registry = registry

    def invoke(self, call: ToolCall) -> ToolResult:
        try:
            env = deployment_proposal_artifact(
                producer=f"agent:{call.agent_id}",
                experiment_id=call.experiment_id,
                parents=tuple(call.arguments.get("evidence_artifact_ids", [])),
                metadata={"proposal": call.arguments.get("proposal", {}),
                          "trace_id": call.trace_id},
            )
            self._registry.register(env)
            return ToolResult(call_id=call.call_id, status="OK",
                              artifact_ids=[env.artifact_id])
        except Exception as exc:  # noqa: BLE001
            return _error(call, exc)


class CodeSandboxTool:
    """Interface placeholder: executing agent code requires a sandbox which is
    intentionally not implemented in V1."""

    name = "code_sandbox"

    def invoke(self, call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.call_id, status="ERROR",
                          error="code sandbox not implemented in V1")
