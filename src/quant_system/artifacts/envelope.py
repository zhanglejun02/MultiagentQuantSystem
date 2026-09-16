"""Immutable artifact envelope.

An artifact is immutable after registration; any modification produces a NEW
artifact carrying parent lineage.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quant_system.domain.enums import ArtifactStatus, ArtifactType
from quant_system.domain.types import new_id, require_aware, utc_now

SCHEMA_VERSION = "1.0"


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_json(obj: Any) -> str:
    return hash_bytes(json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False).encode())


class ArtifactEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: str = Field(default_factory=lambda: new_id("art_"))
    artifact_type: ArtifactType
    schema_version: str = SCHEMA_VERSION
    created_at: datetime = Field(default_factory=utc_now)
    producer: str
    parent_artifact_ids: tuple[str, ...] = ()
    data_snapshot_id: str | None = None
    experiment_id: str | None = None
    code_commit: str = "unknown"
    config_hash: str = ""
    content_hash: str = ""
    uri: str = ""
    metadata: dict[str, Any] = {}
    status: ArtifactStatus = ArtifactStatus.REGISTERED

    @field_validator("created_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return require_aware(v, "created_at")

    def derive(self, **overrides: Any) -> ArtifactEnvelope:
        """New artifact derived from this one, with lineage preserved."""
        data = self.model_dump()
        data.update(overrides)
        data["artifact_id"] = new_id("art_")
        data["created_at"] = utc_now()
        parents = set(data.get("parent_artifact_ids") or ())
        parents.add(self.artifact_id)
        data["parent_artifact_ids"] = tuple(sorted(parents))
        return ArtifactEnvelope(**data)
