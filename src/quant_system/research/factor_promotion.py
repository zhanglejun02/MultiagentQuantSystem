"""Deterministic factor promotion; LLM output is never promotion authority."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.domain.enums import ArtifactStatus, ArtifactType, FactorState
from quant_system.domain.exceptions import InvalidTransitionError
from quant_system.research.factors import ALLOWED_FACTOR_TRANSITIONS, Factor, FactorRegistry


class FactorValidationEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    factor_id: str = ""
    pit_correct: bool = False
    oos_validated: bool = False
    walk_forward_passed: bool = False
    purged_cv_passed: bool = False
    embargo_applied: bool = False
    multiple_testing_corrected: bool = False
    redundancy_checked: bool = False
    neutralization_checked: bool = False
    turnover_acceptable: bool = False
    transaction_cost_acceptable: bool = False
    capacity_acceptable: bool = False
    regime_robust: bool = False
    stress_test_passed: bool = False
    final_holdout_passed: bool = False
    metrics: dict[str, Any] = {}


REQUIRED_EVIDENCE: dict[FactorState, tuple[str, ...]] = {
    FactorState.SCREENED: ("pit_correct",),
    FactorState.CANDIDATE: (
        "pit_correct",
        "oos_validated",
        "multiple_testing_corrected",
        "redundancy_checked",
    ),
    FactorState.VALIDATING: (),
    FactorState.VALIDATED: (
        "pit_correct",
        "oos_validated",
        "walk_forward_passed",
        "purged_cv_passed",
        "embargo_applied",
        "multiple_testing_corrected",
        "redundancy_checked",
        "neutralization_checked",
        "turnover_acceptable",
        "transaction_cost_acceptable",
        "capacity_acceptable",
        "regime_robust",
        "stress_test_passed",
        "final_holdout_passed",
    ),
    FactorState.STRATEGY_ELIGIBLE: (
        "final_holdout_passed",
    ),
}


class FactorPromotionService:
    def __init__(
        self,
        factor_registry: FactorRegistry,
        artifact_registry: ArtifactRegistry,
    ) -> None:
        self._factors = factor_registry
        self._artifacts = artifact_registry

    def promote(
        self,
        factor_id: str,
        target: FactorState,
        validation_artifact_id: str,
    ) -> Factor:
        factor = self._factors.get(factor_id)
        if target not in ALLOWED_FACTOR_TRANSITIONS[factor.state]:
            raise InvalidTransitionError(
                f"factor transition {factor.state} -> {target} not allowed"
            )
        if target == FactorState.DEPRECATED:
            raise InvalidTransitionError("deprecation is an operator action, not promotion")
        artifact = self._artifacts.get(validation_artifact_id)
        if artifact.artifact_type != ArtifactType.VALIDATION_REPORT:
            raise InvalidTransitionError("promotion evidence must be a VALIDATION_REPORT artifact")
        if artifact.status != ArtifactStatus.VERIFIED:
            raise InvalidTransitionError("validation report must be VERIFIED")
        evidence = FactorValidationEvidence.model_validate(artifact.metadata)
        if evidence.factor_id != factor_id:
            raise InvalidTransitionError("validation report is not bound to this factor")
        missing = [
            field
            for field in REQUIRED_EVIDENCE[target]
            if not getattr(evidence, field)
        ]
        if missing:
            raise InvalidTransitionError(
                f"factor promotion to {target} missing evidence: {', '.join(missing)}"
            )
        return self._factors._transition(
            factor_id,
            target,
            reason=f"deterministic policy passed: {', '.join(REQUIRED_EVIDENCE[target]) or 'stage opened'}",
            actor="system:factor_promotion_policy",
            validation_artifact_id=artifact.artifact_id,
            metrics=evidence.metrics,
        )