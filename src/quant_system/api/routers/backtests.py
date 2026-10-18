"""Equity backtest result and paper portfolio routes."""

from __future__ import annotations

from fastapi import APIRouter

from quant_system.api.common import env_to_dict
from quant_system.api.container import ApiContainer
from quant_system.domain.enums import ArtifactType


def build_router(c: ApiContainer) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["backtests"])
    reader = c.backtest_reader

    @router.get("/backtests")
    def list_backtests() -> list[dict]:
        envs = c.registry.find_by_type(ArtifactType.BACKTEST_RESULT)
        return sorted((env_to_dict(e) for e in envs), key=lambda d: d["created_at"], reverse=True)

    @router.get("/backtests/{artifact_id}/metrics")
    def backtest_metrics(artifact_id: str) -> dict:
        return reader.metrics(artifact_id)

    @router.get("/backtests/{artifact_id}/equity")
    def backtest_equity(artifact_id: str) -> list[dict]:
        return reader.equity_curve(artifact_id)

    @router.get("/backtests/{artifact_id}/orders")
    def backtest_orders(artifact_id: str, limit: int = 500) -> list[dict]:
        return reader.orders(artifact_id, limit)

    @router.get("/backtests/{artifact_id}/trades")
    def backtest_trades(artifact_id: str, limit: int = 500) -> list[dict]:
        return reader.trades(artifact_id, limit)

    @router.get("/backtests/{artifact_id}/events")
    def backtest_events(artifact_id: str, event_type: str | None = None, limit: int = 1000) -> list[dict]:
        return reader.events(artifact_id, event_type, limit)

    @router.get("/backtests/{artifact_id}/positions")
    def backtest_positions(artifact_id: str) -> dict:
        return reader.positions(artifact_id)

    @router.get("/portfolio/latest")
    def latest_portfolio() -> dict:
        return reader.latest_positions()

    return router
