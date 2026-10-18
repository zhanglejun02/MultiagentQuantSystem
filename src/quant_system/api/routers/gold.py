"""Gold dashboard, allocation and gold research routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException

from quant_system.api.container import ApiContainer
from quant_system.api.routers.market import latest_artifact, market_snapshot_payload
from quant_system.domain.enums import ArtifactType
from quant_system.domain.exceptions import ArtifactNotFoundError, QuantSystemError
from quant_system.gold.models import Allocation
from quant_system.gold.research import GoldResearchBusy, GoldResearchRequest, GoldResearchRun


def build_router(c: ApiContainer) -> APIRouter:
    router = APIRouter(prefix="/api/gold", tags=["gold"])

    def _gold_dashboard() -> dict[str, Any]:
        try:
            result = c.gold.dashboard()
        except (ValueError, ArtifactNotFoundError, UnicodeDecodeError) as exc:
            raise HTTPException(
                500, "gold data is missing, malformed or failed integrity verification"
            ) from exc
        market = latest_artifact(c.registry, ArtifactType.MARKET_SNAPSHOT)
        if market is not None:
            try:
                market_payload = market_snapshot_payload(c.registry, c.artifact_store, market.artifact_id)[
                    "snapshot"
                ]
                result["futures"] = {
                    "trade_date": market_payload.get("trade_date"),
                    "contracts": [
                        row
                        for row in market_payload.get("futures", {}).get("dominant_contracts", [])
                        if str(row.get("product", "")).lower() == "au"
                    ],
                }
            except (HTTPException, KeyError, TypeError, AttributeError):
                if result["snapshot"]:
                    result["snapshot"]["warnings"].append("辅助期货快照不可读取；黄金行情与配置不受影响。")
        return result

    @router.get("/dashboard")
    def gold_dashboard() -> dict[str, Any]:
        return _gold_dashboard()

    @router.post("/allocation")
    def save_gold_allocation(body: Allocation) -> dict[str, Any]:
        c.gold.save_allocation(body)
        return _gold_dashboard()

    @router.get("/research/status")
    def gold_research_status() -> dict[str, Any]:
        return c.gold_research.status()

    @router.get("/research/runs")
    def list_gold_research_runs() -> list[GoldResearchRun]:
        return c.gold_research.list_runs()

    @router.get("/research/runs/{run_id}")
    def get_gold_research_run(run_id: str) -> GoldResearchRun:
        try:
            return c.gold_research.get(run_id)
        except ArtifactNotFoundError as exc:
            raise HTTPException(404, "gold research run not found") from exc

    @router.post("/research/runs", status_code=202)
    def start_gold_research(body: GoldResearchRequest, background_tasks: BackgroundTasks) -> GoldResearchRun:
        try:
            run = c.gold_research.create(body)
        except GoldResearchBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except QuantSystemError as exc:
            raise HTTPException(503, str(exc)) from exc
        background_tasks.add_task(c.gold_research.execute, run.run_id)
        return run

    return router
