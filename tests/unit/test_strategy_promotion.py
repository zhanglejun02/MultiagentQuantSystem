from __future__ import annotations

import pytest

from quant_system.artifacts.types import (
    backtest_result_artifact,
    portfolio_construction_spec_artifact,
    signal_spec_artifact,
    strategy_package_artifact,
    validation_report_artifact,
)
from quant_system.domain.enums import ArtifactStatus, FactorState, StrategyState
from quant_system.domain.exceptions import InvalidTransitionError, KillSwitchEngagedError
from quant_system.research.factor_promotion import (
    FactorPromotionService,
    FactorValidationEvidence,
)
from quant_system.research.factors import FactorRegistry
from quant_system.research.strategies import (
    StrategyPromotionEvidence,
    StrategyPromotionService,
    StrategyRegistry,
)
from quant_system.risk.runtime import KillSwitch, TradingRuntime


def eligible_factor(factors: FactorRegistry, registry) -> str:
    factor = factors.create(
        factor_key="momentum_20d",
        version="1.0.0",
        name="Momentum 20D",
        category="momentum",
        owner="research",
        code_ref="factor_dsl:run-1",
    )
    evidence = FactorValidationEvidence(
        factor_id=factor.factor_id,
        pit_correct=True,
        oos_validated=True,
        walk_forward_passed=True,
        purged_cv_passed=True,
        embargo_applied=True,
        multiple_testing_corrected=True,
        redundancy_checked=True,
        neutralization_checked=True,
        turnover_acceptable=True,
        transaction_cost_acceptable=True,
        capacity_acceptable=True,
        regime_robust=True,
        stress_test_passed=True,
        final_holdout_passed=True,
    )
    report = registry.register(
        validation_report_artifact(
            producer="factor-validation",
            metadata=evidence.model_dump(mode="json"),
            status=ArtifactStatus.VERIFIED,
        )
    )
    promotion = FactorPromotionService(factors, registry)
    for state in (
        FactorState.SCREENED,
        FactorState.CANDIDATE,
        FactorState.VALIDATING,
        FactorState.VALIDATED,
        FactorState.STRATEGY_ELIGIBLE,
    ):
        factor = promotion.promote(factor.factor_id, state, report.artifact_id)
    return factor.factor_id


def strategy_package(registry):
    signal = registry.register(signal_spec_artifact(producer="strategy-builder"))
    construction = registry.register(
        portfolio_construction_spec_artifact(producer="strategy-builder")
    )
    return registry.register(
        strategy_package_artifact(
            producer="strategy-builder",
            parents=(signal.artifact_id, construction.artifact_id),
            status=ArtifactStatus.VERIFIED,
        )
    )


def test_strategy_lifecycle_requires_evidence_and_human_approval(session_factory, registry):
    factors = FactorRegistry(session_factory)
    strategies = StrategyRegistry(session_factory)
    strategy = strategies.create(
        strategy_key="momentum-topk",
        version="1.0.0",
        package_artifact_id=strategy_package(registry).artifact_id,
        factor_ids=[eligible_factor(factors, registry)],
    )
    backtest = registry.register(
        backtest_result_artifact(producer="backtest", status=ArtifactStatus.VERIFIED)
    )
    validation = registry.register(
        validation_report_artifact(
            producer="strategy-validation",
            metadata={"passed": True},
            status=ArtifactStatus.VERIFIED,
        )
    )
    service = StrategyPromotionService(
        strategies,
        factors,
        registry,
        TradingRuntime(KillSwitch()),
    )

    strategy = service.promote(
        strategy.strategy_id,
        StrategyState.BACKTESTED,
        StrategyPromotionEvidence(backtest_result_artifact_id=backtest.artifact_id),
    )
    strategy = service.promote(
        strategy.strategy_id,
        StrategyState.VALIDATED,
        StrategyPromotionEvidence(validation_report_artifact_id=validation.artifact_id),
    )
    with pytest.raises(InvalidTransitionError, match="human approval"):
        service.promote(strategy.strategy_id, StrategyState.APPROVED, StrategyPromotionEvidence())
    strategy = service.promote(
        strategy.strategy_id,
        StrategyState.APPROVED,
        StrategyPromotionEvidence(approved_by="risk-committee:user-1"),
    )
    assert strategy.state == StrategyState.APPROVED


def test_runtime_blocks_strategy_activation(session_factory, registry):
    factors = FactorRegistry(session_factory)
    strategies = StrategyRegistry(session_factory)
    strategy = strategies.create(
        strategy_key="halted-strategy",
        version="1.0.0",
        package_artifact_id=strategy_package(registry).artifact_id,
        factor_ids=[eligible_factor(factors, registry)],
    )
    strategies._transition(
        strategy.strategy_id,
        StrategyState.BACKTESTED,
        reason="fixture",
        actor="test",
    )
    strategies._transition(
        strategy.strategy_id,
        StrategyState.VALIDATED,
        reason="fixture",
        actor="test",
    )
    strategies._transition(
        strategy.strategy_id,
        StrategyState.APPROVED,
        reason="fixture",
        actor="human:test",
    )
    kill_switch = KillSwitch()
    kill_switch.engage("incident")
    service = StrategyPromotionService(
        strategies,
        factors,
        registry,
        TradingRuntime(kill_switch),
    )

    with pytest.raises(KillSwitchEngagedError):
        service.promote(strategy.strategy_id, StrategyState.PAPER, StrategyPromotionEvidence())


def test_strategy_package_requires_signal_and_construction_lineage(session_factory, registry):
    factors = FactorRegistry(session_factory)
    strategies = StrategyRegistry(session_factory)
    incomplete = registry.register(
        strategy_package_artifact(producer="strategy-builder", status=ArtifactStatus.VERIFIED)
    )
    strategy = strategies.create(
        strategy_key="incomplete",
        version="1.0.0",
        package_artifact_id=incomplete.artifact_id,
        factor_ids=[eligible_factor(factors, registry)],
    )
    backtest = registry.register(
        backtest_result_artifact(producer="backtest", status=ArtifactStatus.VERIFIED)
    )

    with pytest.raises(InvalidTransitionError, match="signal/portfolio construction"):
        StrategyPromotionService(
            strategies,
            factors,
            registry,
            TradingRuntime(KillSwitch()),
        ).promote(
            strategy.strategy_id,
            StrategyState.BACKTESTED,
            StrategyPromotionEvidence(backtest_result_artifact_id=backtest.artifact_id),
        )