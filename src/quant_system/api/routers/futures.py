"""Futures paper simulation and futures backtest routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from quant_system.api.common import env_to_dict
from quant_system.api.container import ApiContainer
from quant_system.api.schemas import FuturesOrderCreate, FuturesSimulationCreate
from quant_system.domain.enums import ArtifactType
from quant_system.domain.types import new_id, utc_now
from quant_system.futures.backtest import FuturesBacktestRequest
from quant_system.futures.domain import FuturesOrderIntent


def build_router(c: ApiContainer) -> APIRouter:
    router = APIRouter(prefix="/api/futures", tags=["futures"])
    reader = c.futures_backtest_reader

    def _futures_call(callable_, *args, **kwargs):
        try:
            return callable_(*args, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/contracts")
    def futures_contracts() -> list[dict]:
        return c.futures.contracts()

    @router.get("/backtests")
    def list_futures_backtests() -> list[dict]:
        envs = c.registry.find_by_type(ArtifactType.FUTURES_BACKTEST_RESULT)
        return sorted(
            (env_to_dict(envelope) for envelope in envs),
            key=lambda item: item["created_at"],
            reverse=True,
        )

    @router.post("/backtests", status_code=201)
    def run_futures_backtest(body: FuturesBacktestRequest) -> dict:
        return _futures_call(c.futures_backtest_engine.run, body).model_dump(mode="json")

    @router.get("/backtests/{artifact_id}/metrics")
    def futures_backtest_metrics(artifact_id: str) -> dict:
        return reader.metrics(artifact_id)

    @router.get("/backtests/{artifact_id}/equity")
    def futures_backtest_equity(artifact_id: str) -> list[dict]:
        return reader.equity_curve(artifact_id)

    @router.get("/simulations")
    def futures_simulations() -> list[dict]:
        return c.futures.list_simulations()

    @router.post("/simulations", status_code=201)
    def create_futures_simulation(body: FuturesSimulationCreate) -> dict:
        return _futures_call(
            c.futures.create,
            name=body.name,
            initial_balance=body.initial_balance,
            mode=body.mode,
        )

    @router.get("/simulations/{simulation_id}")
    def futures_snapshot(simulation_id: str) -> dict:
        return _futures_call(c.futures.snapshot, simulation_id)

    @router.get("/simulations/{simulation_id}/positions")
    def futures_positions(simulation_id: str) -> list[dict]:
        return _futures_call(c.futures.snapshot, simulation_id)["positions"]

    @router.get("/simulations/{simulation_id}/orders")
    def futures_orders(simulation_id: str) -> list[dict]:
        return _futures_call(c.futures.orders, simulation_id)

    @router.get("/simulations/{simulation_id}/trades")
    def futures_trades(simulation_id: str) -> list[dict]:
        return _futures_call(c.futures.trades, simulation_id)

    @router.get("/simulations/{simulation_id}/settlements")
    def futures_settlements(simulation_id: str) -> list[dict]:
        return _futures_call(c.futures.settlements, simulation_id)

    @router.get("/simulations/{simulation_id}/events")
    def futures_events(simulation_id: str) -> list[dict]:
        return _futures_call(c.futures.events, simulation_id)

    @router.post("/simulations/{simulation_id}/orders", status_code=201)
    def submit_futures_order(simulation_id: str, body: FuturesOrderCreate) -> dict:
        intent = FuturesOrderIntent(
            client_order_id=body.client_order_id or new_id("fcmd_"),
            symbol=body.symbol,
            direction=body.direction,
            offset=body.offset,
            quantity=body.quantity,
            order_type=body.order_type,
            limit_price=body.limit_price,
            created_at=utc_now(),
        )
        return _futures_call(c.futures.submit_order, simulation_id, intent)

    @router.post("/simulations/{simulation_id}/orders/{order_id}/cancel")
    def cancel_futures_order(simulation_id: str, order_id: str) -> dict:
        return _futures_call(c.futures.cancel_order, simulation_id, order_id)

    @router.post("/simulations/{simulation_id}/advance")
    def advance_futures_simulation(simulation_id: str) -> dict:
        return _futures_call(c.futures.advance, simulation_id)

    return router
