"""Artifact registry and lineage routes."""

from __future__ import annotations

from fastapi import APIRouter, Query

from quant_system.api.common import env_to_dict
from quant_system.api.container import ApiContainer
from quant_system.domain.enums import ArtifactType


def build_router(c: ApiContainer) -> APIRouter:
    router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])

    @router.get("")
    def list_artifacts(artifact_type: str | None = Query(default=None)) -> list[dict]:
        if artifact_type:
            envs = c.registry.find_by_type(ArtifactType(artifact_type))
        else:
            envs = c.registry.list_all()
        return [env_to_dict(e) for e in envs]

    @router.get("/{artifact_id}")
    def get_artifact(artifact_id: str) -> dict:
        return env_to_dict(c.registry.get(artifact_id))

    @router.get("/{artifact_id}/lineage")
    def get_lineage(artifact_id: str) -> dict:
        graph = c.lineage.lineage(artifact_id)
        return {
            "ancestors": [env_to_dict(a) for a in graph["ancestors"]],
            "descendants": [env_to_dict(d) for d in graph["descendants"]],
        }

    return router
