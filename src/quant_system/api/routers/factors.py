"""Factor registry and FactorLab routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from quant_system.api.container import ApiContainer
from quant_system.api.schemas import (
    FactorCreate,
    FactorEvaluateRequest,
    FactorPromotionRequest,
    FactorTransitionRequest,
)
from quant_system.domain.exceptions import InvalidTransitionError, QuantSystemError


def build_router(c: ApiContainer) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["factors"])

    @router.get("/factors")
    def list_factors() -> list[dict]:
        return [factor.model_dump(mode="json") for factor in c.factors.list_all()]

    @router.post("/factors", status_code=201)
    def create_factor(body: FactorCreate) -> dict:
        try:
            factor = c.factors.create(**body.model_dump())
        except QuantSystemError as exc:
            raise HTTPException(409, str(exc)) from exc
        return factor.model_dump(mode="json")

    @router.post("/factors/{factor_id}/transition")
    def transition_factor(factor_id: str, body: FactorTransitionRequest) -> dict:
        try:
            factor = c.factors.transition(factor_id, body.target, reason=body.reason, actor=body.actor)
        except InvalidTransitionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except QuantSystemError as exc:
            raise HTTPException(404, str(exc)) from exc
        return factor.model_dump(mode="json")

    @router.post("/factors/{factor_id}/promote")
    def promote_factor(factor_id: str, body: FactorPromotionRequest) -> dict:
        try:
            factor = c.factor_promotion.promote(factor_id, body.target, body.validation_artifact_id)
        except QuantSystemError as exc:
            raise HTTPException(409, str(exc)) from exc
        return factor.model_dump(mode="json")

    @router.get("/factors/{factor_id}/history")
    def factor_history(factor_id: str) -> list[dict]:
        return c.factors.history(factor_id)

    @router.get("/factor-runs")
    def list_factor_runs() -> list[dict]:
        return [run.model_dump(mode="json") for run in c.factor_lab.list_runs()]

    @router.post("/factor-runs/evaluate", status_code=201)
    def evaluate_factor(body: FactorEvaluateRequest) -> dict:
        try:
            run = c.factor_lab.evaluate(**body.model_dump())
        except (QuantSystemError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return run.model_dump(mode="json")

    return router
