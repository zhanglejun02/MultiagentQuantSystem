from __future__ import annotations

import pytest

from quant_system.artifacts.types import research_report_artifact, validation_report_artifact
from quant_system.domain.enums import ArtifactStatus, FactorState
from quant_system.domain.exceptions import InvalidTransitionError
from quant_system.research.factor_promotion import (
    FactorPromotionService,
    FactorValidationEvidence,
)
from quant_system.research.factors import FactorRegistry


def create_factor(registry: FactorRegistry):
    return registry.create(
        factor_key="momentum_20d",
        version="1.0.0",
        name="Momentum 20D",
        category="momentum",
        owner="research",
        code_ref="factor_dsl:run-1",
    )


def complete_evidence(factor_id: str) -> FactorValidationEvidence:
    return FactorValidationEvidence(
    factor_id=factor_id,
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
        metrics={"rank_ic": 0.04},
    )


def register_evidence(registry, evidence: FactorValidationEvidence):
    return registry.register(
        validation_report_artifact(
            producer="deterministic-validation-pipeline",
            metadata=evidence.model_dump(mode="json"),
            status=ArtifactStatus.VERIFIED,
        )
    )


def test_positive_transition_cannot_bypass_promotion_policy(session_factory):
    registry = FactorRegistry(session_factory)
    factor = create_factor(registry)

    with pytest.raises(InvalidTransitionError, match="FactorPromotionService"):
        registry.transition(factor.factor_id, FactorState.SCREENED, actor="agent:researcher")


def test_factor_requires_complete_deterministic_evidence(session_factory, registry):
    factors = FactorRegistry(session_factory)
    factor = create_factor(factors)
    promotion = FactorPromotionService(factors, registry)

    factor = promotion.promote(
        factor.factor_id,
        FactorState.SCREENED,
        register_evidence(
            registry,
            FactorValidationEvidence(factor_id=factor.factor_id, pit_correct=True),
        ).artifact_id,
    )
    with pytest.raises(InvalidTransitionError, match="oos_validated"):
        promotion.promote(
            factor.factor_id,
            FactorState.CANDIDATE,
            register_evidence(
                registry,
                FactorValidationEvidence(factor_id=factor.factor_id, pit_correct=True),
            ).artifact_id,
        )


def test_complete_evidence_reaches_strategy_eligible(session_factory, registry):
    factors = FactorRegistry(session_factory)
    factor = create_factor(factors)
    promotion = FactorPromotionService(factors, registry)
    evidence = complete_evidence(factor.factor_id)
    artifact = register_evidence(registry, evidence)

    for state in (
        FactorState.SCREENED,
        FactorState.CANDIDATE,
        FactorState.VALIDATING,
        FactorState.VALIDATED,
        FactorState.STRATEGY_ELIGIBLE,
    ):
        factor = promotion.promote(factor.factor_id, state, artifact.artifact_id)

    assert factor.state == FactorState.STRATEGY_ELIGIBLE
    assert factor.validation_artifact_id == artifact.artifact_id
    assert factor.metrics == {"rank_ic": 0.04}
    assert {item["actor"] for item in factors.history(factor.factor_id)} == {
        "system:factor_promotion_policy"
    }


def test_non_validation_artifact_cannot_promote(session_factory, registry):
    factors = FactorRegistry(session_factory)
    factor = create_factor(factors)
    report = registry.register(research_report_artifact(producer="agent:researcher"))

    with pytest.raises(InvalidTransitionError, match="VALIDATION_REPORT"):
        FactorPromotionService(factors, registry).promote(
            factor.factor_id,
            FactorState.SCREENED,
            report.artifact_id,
        )