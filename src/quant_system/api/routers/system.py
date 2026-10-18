"""Health, system status and kill switch routes."""

from __future__ import annotations

from fastapi import APIRouter

from quant_system.api.container import ApiContainer
from quant_system.domain.enums import ArtifactType
from quant_system.domain.types import utc_now


def build_router(c: ApiContainer) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["system"])

    @router.get("/health")
    def health() -> dict:
        db_ok = True
        try:
            c.registry.find_by_type(ArtifactType.DATA_SNAPSHOT)
        except Exception:
            db_ok = False
        return {
            "status": "ok" if db_ok else "degraded",
            "db": "ok" if db_ok else "unreachable",
            "time": utc_now().isoformat(),
        }

    @router.get("/system/status")
    def system_status() -> dict:
        snapshots = c.registry.find_by_type(ArtifactType.DATA_SNAPSHOT)
        results = c.registry.find_by_type(ArtifactType.BACKTEST_RESULT)
        portfolio_snapshot = c.portfolio_risk.snapshot()
        return {
            "runtime_state": c.trading_runtime.state.value,
            "kill_switch": c.kill_switch.engaged,
            "kill_switch_reason": c.kill_switch.reason,
            "safe_mode_reason": c.trading_runtime.safe_mode_reason,
            "n_data_snapshots": len(snapshots),
            "n_backtest_results": len(results),
            "n_futures_simulations": len(c.futures.list_simulations()),
            "n_futures_backtests": len(c.registry.find_by_type(ArtifactType.FUTURES_BACKTEST_RESULT)),
            "futures_gateway": "LOCAL_PAPER",
            "paper_broker": "SIMULATED",
            "oms": "IDLE",
            "portfolio_risk": {
                "total_nav": str(portfolio_snapshot.total_nav),
                "gross_exposure": str(portfolio_snapshot.gross_exposure),
                "net_exposure": str(portfolio_snapshot.net_exposure),
                "margin": str(portfolio_snapshot.margin),
                "strategy_gross_exposure": {
                    key: str(value) for key, value in portfolio_snapshot.strategy_gross_exposure.items()
                },
                "asset_class_gross_exposure": {
                    key: str(value) for key, value in portfolio_snapshot.asset_class_gross_exposure.items()
                },
            },
            "heartbeat": utc_now().isoformat(),
        }

    @router.post("/system/killswitch")
    def toggle_killswitch(engage: bool, reason: str = "manual", operator: str = "dashboard") -> dict:
        if engage:
            c.kill_switch.engage(reason)
        else:
            c.kill_switch.disengage(operator)
        return {"kill_switch": c.kill_switch.engaged, "reason": c.kill_switch.reason}

    return router
