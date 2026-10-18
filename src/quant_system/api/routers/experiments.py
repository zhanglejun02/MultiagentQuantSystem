"""Experiment lifecycle routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from quant_system.api.container import ApiContainer
from quant_system.api.schemas import ExperimentCreate, TransitionRequest
from quant_system.domain.exceptions import InvalidTransitionError, QuantSystemError


def build_router(c: ApiContainer) -> APIRouter:
    router = APIRouter(prefix="/api/experiments", tags=["experiments"])

    @router.get("")
    def list_experiments() -> list[dict]:
        return [e.model_dump(mode="json") for e in c.experiments.list_all()]

    @router.post("")
    def create_experiment(body: ExperimentCreate) -> dict:
        return c.experiments.create(body.name, body.description).model_dump(mode="json")

    @router.post("/{experiment_id}/transition")
    def transition_experiment(experiment_id: str, body: TransitionRequest) -> dict:
        try:
            exp = c.experiments.transition(experiment_id, body.target, reason=body.reason, actor=body.actor)
        except InvalidTransitionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except QuantSystemError as exc:
            raise HTTPException(404, str(exc)) from exc
        return exp.model_dump(mode="json")

    @router.get("/{experiment_id}/history")
    def experiment_history(experiment_id: str) -> list[dict]:
        return c.experiments.history(experiment_id)

    @router.get("/{experiment_id}/runs")
    def experiment_runs(experiment_id: str) -> list[dict]:
        return [r.model_dump(mode="json") for r in c.experiments.runs(experiment_id)]

    return router
