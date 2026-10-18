"""Shared serialization helpers for API routers."""

from __future__ import annotations

from typing import Any

from quant_system.artifacts.envelope import ArtifactEnvelope


def env_to_dict(env: ArtifactEnvelope) -> dict[str, Any]:
    return env.model_dump(mode="json")
