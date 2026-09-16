"""PromotionPolicy: hard validation gates (fail-closed) separated from the
performance scorecard. Thresholds come from configuration, never code."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel

from quant_system.artifacts.lineage import ArtifactLineageService
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.data.snapshots.service import DataSnapshotService
from quant_system.domain.enums import ArtifactType
from quant_system.domain.exceptions import ArtifactNotFoundError
from quant_system.research.scorecard import Scorecard, build_scorecard


class GateResult(BaseModel):
    gate: str
    passed: bool
    detail: str = ""


class ValidationReport(BaseModel):
    passed: bool
    gate_results: list[GateResult]
    scorecard: Scorecard
    threshold_failures: list[str]


class ValidationGate(Protocol):
    name: str

    def check(self, result_metadata: dict[str, Any], context: dict[str, Any]) -> GateResult: ...


class LineageGate:
    name = "broken_artifact_lineage"

    def __init__(self, lineage: ArtifactLineageService) -> None:
        self._lineage = lineage

    def check(self, result_metadata: dict[str, Any], context: dict[str, Any]) -> GateResult:
        artifact_id = context.get("result_artifact_id", "")
        try:
            self._lineage.verify_lineage(artifact_id)
            parents = self._lineage.parents(artifact_id)
            has_spec = any(p.artifact_type == ArtifactType.BACKTEST_SPEC for p in parents)
            if not has_spec:
                return GateResult(gate=self.name, passed=False,
                                  detail="result has no BACKTEST_SPEC parent")
            return GateResult(gate=self.name, passed=True)
        except ArtifactNotFoundError as exc:
            return GateResult(gate=self.name, passed=False, detail=str(exc))


class SnapshotIntegrityGate:
    """Guards against unreproducible results: the pinned data must not have drifted."""

    name = "unreproducible_result"

    def __init__(self, registry: ArtifactRegistry, snapshots: DataSnapshotService) -> None:
        self._registry = registry
        self._snapshots = snapshots

    def check(self, result_metadata: dict[str, Any], context: dict[str, Any]) -> GateResult:
        snapshot_id = context.get("data_snapshot_id")
        if not snapshot_id:
            return GateResult(gate=self.name, passed=False, detail="no data snapshot pinned")
        try:
            snapshot = self._registry.get(snapshot_id)
        except ArtifactNotFoundError:
            return GateResult(gate=self.name, passed=False,
                              detail=f"snapshot {snapshot_id} not registered")
        if not self._snapshots.verify_snapshot(snapshot):
            return GateResult(gate=self.name, passed=False, detail="snapshot content drifted")
        return GateResult(gate=self.name, passed=True)


class CostConfigGate:
    name = "invalid_cost_config"

    def check(self, result_metadata: dict[str, Any], context: dict[str, Any]) -> GateResult:
        request = context.get("request", {})
        if not request.get("fee_model"):
            return GateResult(gate=self.name, passed=False, detail="no fee model configured")
        slippage = request.get("slippage_model") or {}
        if "bps" not in slippage and slippage.get("name") != "zero":
            return GateResult(gate=self.name, passed=False, detail="no slippage configured")
        return GateResult(gate=self.name, passed=True)


class ExecutionAssumptionGate:
    """Signals from T close must execute no earlier than T+1."""

    name = "invalid_execution_assumptions"

    def check(self, result_metadata: dict[str, Any], context: dict[str, Any]) -> GateResult:
        request = context.get("request", {})
        if request.get("execution_model") != "next_open":
            return GateResult(gate=self.name, passed=False,
                              detail=f"unvalidated execution model {request.get('execution_model')}")
        return GateResult(gate=self.name, passed=True)


class CriticalRiskGate:
    name = "critical_risk_violation"

    def check(self, result_metadata: dict[str, Any], context: dict[str, Any]) -> GateResult:
        critical = [v for v in context.get("violations", []) if "KillSwitch" in v]
        if critical:
            return GateResult(gate=self.name, passed=False, detail="; ".join(critical[:5]))
        return GateResult(gate=self.name, passed=True)


class ReplayGate:
    """PIT/leakage smoke check: the event log must replay to identical state."""

    name = "pit_violation"

    def check(self, result_metadata: dict[str, Any], context: dict[str, Any]) -> GateResult:
        replay_ok = context.get("replay_ok")
        if replay_ok is not True:
            return GateResult(gate=self.name, passed=False,
                              detail="event replay not verified or diverged")
        return GateResult(gate=self.name, passed=True)


class PromotionPolicy:
    def __init__(self, gates: list[ValidationGate], thresholds: dict[str, Any]) -> None:
        self._gates = gates
        self._thresholds = thresholds

    @staticmethod
    def load_thresholds(config_path: str) -> dict[str, Any]:
        raw = yaml.safe_load(Path(config_path).read_text())
        return raw.get("scorecard_thresholds", {})

    def validate(
        self,
        *,
        performance: dict[str, Any],
        execution: dict[str, Any],
        context: dict[str, Any],
    ) -> ValidationReport:
        gate_results = [g.check({}, context) for g in self._gates]
        hard_pass = all(g.passed for g in gate_results)

        scorecard = build_scorecard(performance, execution)
        threshold_failures: list[str] = []
        min_sharpe = self._thresholds.get("min_sharpe")
        if min_sharpe is not None and scorecard.sharpe is not None:
            if scorecard.sharpe < float(Decimal(str(min_sharpe))):
                threshold_failures.append(f"sharpe {scorecard.sharpe:.3f} < {min_sharpe}")
        max_dd = self._thresholds.get("max_drawdown")
        if max_dd is not None and scorecard.max_drawdown is not None:
            if abs(scorecard.max_drawdown) > float(Decimal(str(max_dd))):
                threshold_failures.append(
                    f"max drawdown {scorecard.max_drawdown:.3f} exceeds {max_dd}"
                )
        min_fill = self._thresholds.get("min_fill_ratio")
        if min_fill is not None and scorecard.fill_ratio is not None:
            if scorecard.fill_ratio < float(Decimal(str(min_fill))):
                threshold_failures.append(f"fill ratio {scorecard.fill_ratio:.3f} < {min_fill}")

        return ValidationReport(
            passed=hard_pass and not threshold_failures,
            gate_results=gate_results,
            scorecard=scorecard,
            threshold_failures=threshold_failures,
        )
