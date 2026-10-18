"""Strategy registry and promotion routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from quant_system.api.container import ApiContainer
from quant_system.api.schemas import StrategyCreate, StrategyPromotionRequest
from quant_system.domain.exceptions import QuantSystemError
from quant_system.research.strategies import StrategyPromotionEvidence


def build_router(c: ApiContainer) -> APIRouter:
    router = APIRouter(prefix="/api/strategies", tags=["strategies"])

    @router.post("", status_code=201)
    def create_strategy(body: StrategyCreate) -> dict:
        try:
            strategy = c.strategies.create(**body.model_dump())
        except QuantSystemError as exc:
            raise HTTPException(409, str(exc)) from exc
        return strategy.model_dump(mode="json")

    @router.post("/{strategy_id}/promote")
    def promote_strategy(strategy_id: str, body: StrategyPromotionRequest) -> dict:
        try:
            strategy = c.strategy_promotion.promote(
                strategy_id,
                body.target,
                StrategyPromotionEvidence(
                    backtest_result_artifact_id=body.backtest_result_artifact_id,
                    validation_report_artifact_id=body.validation_report_artifact_id,
                    approved_by=body.approved_by,
                ),
            )
        except QuantSystemError as exc:
            raise HTTPException(409, str(exc)) from exc
        return strategy.model_dump(mode="json")

    @router.get("/{strategy_id}/history")
    def strategy_history(strategy_id: str) -> list[dict]:
        return c.strategies.history(strategy_id)

    return router
