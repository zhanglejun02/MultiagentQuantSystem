"""Persistent strategy packages and deployment lifecycle policy."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.domain.enums import ArtifactStatus, ArtifactType, FactorState, StrategyState
from quant_system.domain.exceptions import InvalidTransitionError, QuantSystemError
from quant_system.domain.types import new_id, utc_now
from quant_system.infra.db import session_scope
from quant_system.infra.orm import StrategyRow, StrategyTransitionRow
from quant_system.research.factors import FactorRegistry
from quant_system.risk.runtime import TradingRuntime

ALLOWED_STRATEGY_TRANSITIONS: dict[StrategyState, frozenset[StrategyState]] = {
    StrategyState.DRAFT: frozenset({StrategyState.BACKTESTED, StrategyState.RETIRED}),
    StrategyState.BACKTESTED: frozenset({StrategyState.VALIDATED, StrategyState.RETIRED}),
    StrategyState.VALIDATED: frozenset({StrategyState.APPROVED, StrategyState.RETIRED}),
    StrategyState.APPROVED: frozenset({StrategyState.PAPER, StrategyState.RETIRED}),
    StrategyState.PAPER: frozenset({StrategyState.SHADOW, StrategyState.SUSPENDED, StrategyState.RETIRED}),
    StrategyState.SHADOW: frozenset({StrategyState.CANARY, StrategyState.SUSPENDED, StrategyState.RETIRED}),
    StrategyState.CANARY: frozenset({StrategyState.LIVE, StrategyState.SUSPENDED, StrategyState.RETIRED}),
    StrategyState.LIVE: frozenset({StrategyState.SUSPENDED, StrategyState.RETIRED}),
    StrategyState.SUSPENDED: frozenset({StrategyState.PAPER, StrategyState.RETIRED}),
    StrategyState.RETIRED: frozenset(),
}


class StrategyRecord(BaseModel):
    strategy_id: str
    strategy_key: str
    version: str
    state: StrategyState
    package_artifact_id: str
    factor_ids: list[str]
    created_at: str
    updated_at: str


class StrategyPromotionEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    backtest_result_artifact_id: str = ""
    validation_report_artifact_id: str = ""
    approved_by: str = ""


class StrategyRegistry:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    @staticmethod
    def _to_record(row: StrategyRow) -> StrategyRecord:
        return StrategyRecord(
            strategy_id=row.strategy_id,
            strategy_key=row.strategy_key,
            version=row.version,
            state=StrategyState(row.state),
            package_artifact_id=row.package_artifact_id,
            factor_ids=list(row.factor_ids or []),
            created_at=row.created_at.isoformat(),
            updated_at=row.updated_at.isoformat(),
        )

    def create(
        self,
        *,
        strategy_key: str,
        version: str,
        package_artifact_id: str,
        factor_ids: list[str],
    ) -> StrategyRecord:
        now = utc_now()
        with session_scope(self._sf) as session:
            existing = session.execute(
                select(StrategyRow).where(
                    StrategyRow.strategy_key == strategy_key,
                    StrategyRow.version == version,
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise QuantSystemError(f"strategy {strategy_key}@{version} already exists")
            row = StrategyRow(
                strategy_id=new_id("strat_"),
                strategy_key=strategy_key,
                version=version,
                state=StrategyState.DRAFT.value,
                package_artifact_id=package_artifact_id,
                factor_ids=factor_ids,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._to_record(row)

    def get(self, strategy_id: str) -> StrategyRecord:
        with session_scope(self._sf) as session:
            row = session.get(StrategyRow, strategy_id)
            if row is None:
                raise QuantSystemError(f"strategy {strategy_id} not found")
            return self._to_record(row)

    def transition(
        self,
        strategy_id: str,
        target: StrategyState,
        *,
        reason: str,
        actor: str,
    ) -> StrategyRecord:
        if target not in {StrategyState.SUSPENDED, StrategyState.RETIRED}:
            raise InvalidTransitionError(
                "positive strategy promotion is only allowed through StrategyPromotionService"
            )
        return self._transition(strategy_id, target, reason=reason, actor=actor)

    def _transition(
        self,
        strategy_id: str,
        target: StrategyState,
        *,
        reason: str,
        actor: str,
    ) -> StrategyRecord:
        with session_scope(self._sf) as session:
            row = session.get(StrategyRow, strategy_id)
            if row is None:
                raise QuantSystemError(f"strategy {strategy_id} not found")
            current = StrategyState(row.state)
            if target not in ALLOWED_STRATEGY_TRANSITIONS[current]:
                raise InvalidTransitionError(f"strategy transition {current} -> {target} not allowed")
            row.state = target.value
            row.updated_at = utc_now()
            session.add(StrategyTransitionRow(
                strategy_id=strategy_id,
                from_state=current.value,
                to_state=target.value,
                reason=reason,
                actor=actor,
                at=row.updated_at,
            ))
        return self.get(strategy_id)

    def history(self, strategy_id: str) -> list[dict[str, str]]:
        self.get(strategy_id)
        with session_scope(self._sf) as session:
            rows = session.execute(
                select(StrategyTransitionRow)
                .where(StrategyTransitionRow.strategy_id == strategy_id)
                .order_by(StrategyTransitionRow.id)
            ).scalars().all()
            return [
                {
                    "from": row.from_state,
                    "to": row.to_state,
                    "reason": row.reason,
                    "actor": row.actor,
                    "at": row.at.isoformat(),
                }
                for row in rows
            ]


class StrategyPromotionService:
    def __init__(
        self,
        strategies: StrategyRegistry,
        factors: FactorRegistry,
        artifacts: ArtifactRegistry,
        runtime: TradingRuntime,
    ) -> None:
        self._strategies = strategies
        self._factors = factors
        self._artifacts = artifacts
        self._runtime = runtime

    def promote(
        self,
        strategy_id: str,
        target: StrategyState,
        evidence: StrategyPromotionEvidence,
    ) -> StrategyRecord:
        strategy = self._strategies.get(strategy_id)
        self._require_eligible_factors(strategy.factor_ids)
        self._require_package_lineage(strategy.package_artifact_id)
        if target == StrategyState.BACKTESTED:
            self._require_artifact(
                evidence.backtest_result_artifact_id, ArtifactType.BACKTEST_RESULT
            )
        elif target == StrategyState.VALIDATED:
            report = self._require_artifact(
                evidence.validation_report_artifact_id, ArtifactType.VALIDATION_REPORT
            )
            if report.metadata.get("passed") is not True:
                raise InvalidTransitionError("strategy validation report did not pass")
        elif target == StrategyState.APPROVED:
            if not evidence.approved_by:
                raise InvalidTransitionError("human approval identity is required")
        elif target in {
            StrategyState.PAPER,
            StrategyState.SHADOW,
            StrategyState.CANARY,
            StrategyState.LIVE,
        }:
            self._runtime.require_strategy_activation_allowed()
        return self._strategies._transition(
            strategy_id,
            target,
            reason=f"strategy promotion policy passed for {target}",
            actor=evidence.approved_by or "system:strategy_promotion_policy",
        )

    def _require_eligible_factors(self, factor_ids: list[str]) -> None:
        if not factor_ids:
            raise InvalidTransitionError("strategy must reference at least one factor")
        ineligible = [
            factor_id
            for factor_id in factor_ids
            if self._factors.get(factor_id).state != FactorState.STRATEGY_ELIGIBLE
        ]
        if ineligible:
            raise InvalidTransitionError(f"strategy references ineligible factors: {ineligible}")

    def _require_package_lineage(self, package_id: str) -> None:
        package = self._require_artifact(package_id, ArtifactType.STRATEGY_PACKAGE)
        parent_types = {parent.artifact_type for parent in self._artifacts.parents(package.artifact_id)}
        required = {ArtifactType.SIGNAL_SPEC, ArtifactType.PORTFOLIO_CONSTRUCTION_SPEC}
        if not required.issubset(parent_types):
            raise InvalidTransitionError("strategy package lacks signal/portfolio construction lineage")

    def _require_artifact(self, artifact_id: str, artifact_type: ArtifactType):
        if not artifact_id:
            raise InvalidTransitionError(f"{artifact_type} artifact is required")
        artifact = self._artifacts.get(artifact_id)
        if artifact.artifact_type != artifact_type or artifact.status != ArtifactStatus.VERIFIED:
            raise InvalidTransitionError(f"verified {artifact_type} artifact is required")
        return artifact