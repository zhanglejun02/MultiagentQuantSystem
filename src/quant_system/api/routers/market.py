"""Daily market data snapshot routes."""

from __future__ import annotations

from datetime import date
from json import JSONDecodeError
from typing import Any

from fastapi import APIRouter, HTTPException

from quant_system.api.common import env_to_dict
from quant_system.api.container import ApiContainer
from quant_system.artifacts.envelope import ArtifactEnvelope
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.domain.enums import ArtifactType
from quant_system.domain.exceptions import ArtifactNotFoundError
from quant_system.domain.types import utc_now


def latest_artifact(registry: ArtifactRegistry, artifact_type: ArtifactType) -> ArtifactEnvelope | None:
    artifacts = registry.find_by_type(artifact_type)
    return max(artifacts, key=lambda artifact: artifact.created_at) if artifacts else None


def market_snapshot_payload(
    registry: ArtifactRegistry, store: LocalArtifactStore, artifact_id: str
) -> dict[str, Any]:
    try:
        artifact = registry.get(artifact_id)
    except ArtifactNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    if artifact.artifact_type != ArtifactType.MARKET_SNAPSHOT:
        raise HTTPException(400, f"{artifact_id} is not a market snapshot")
    try:
        payload = store.get_json(artifact.uri)
    except ArtifactNotFoundError as exc:
        raise HTTPException(500, f"market snapshot content is missing: {artifact.uri}") from exc
    except (JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(500, f"market snapshot content is malformed: {artifact.uri}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(500, f"market snapshot content is not an object: {artifact.uri}")
    return {"artifact": env_to_dict(artifact), "snapshot": payload}


def build_router(c: ApiContainer) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["market"])

    @router.get("/market-data/status")
    def market_data_status() -> dict[str, Any]:
        quality = latest_artifact(c.registry, ArtifactType.DATA_QUALITY_REPORT)
        snapshot = latest_artifact(c.registry, ArtifactType.MARKET_SNAPSHOT)
        if snapshot is not None:
            market_snapshot_payload(c.registry, c.artifact_store, snapshot.artifact_id)
        latest_trade_date = snapshot.metadata.get("trade_date") if snapshot else None
        stale = True
        if isinstance(latest_trade_date, str):
            stale = (utc_now().date() - date.fromisoformat(latest_trade_date)).days > 4
        return {
            "ready": snapshot is not None,
            "latest_trade_date": latest_trade_date,
            "stale": stale,
            "quality_status": quality.status.value if quality else None,
            "quality_report_id": quality.artifact_id if quality else None,
            "market_snapshot_id": snapshot.artifact_id if snapshot else None,
            "data_snapshot_id": snapshot.data_snapshot_id if snapshot else None,
        }

    @router.get("/market-snapshots")
    def list_market_snapshots() -> list[dict[str, Any]]:
        snapshots = c.registry.find_by_type(ArtifactType.MARKET_SNAPSHOT)
        return sorted(
            (env_to_dict(snapshot) for snapshot in snapshots),
            key=lambda item: item["created_at"],
            reverse=True,
        )

    @router.get("/market-snapshots/latest")
    def latest_market_snapshot() -> dict[str, Any]:
        snapshot = latest_artifact(c.registry, ArtifactType.MARKET_SNAPSHOT)
        if snapshot is None:
            raise HTTPException(404, "no market snapshots")
        return market_snapshot_payload(c.registry, c.artifact_store, snapshot.artifact_id)

    @router.get("/market-snapshots/{artifact_id}")
    def get_market_snapshot(artifact_id: str) -> dict[str, Any]:
        return market_snapshot_payload(c.registry, c.artifact_store, artifact_id)

    return router
