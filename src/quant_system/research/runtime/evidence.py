from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import unquote, urlparse

from quant_system.artifacts.envelope import ArtifactEnvelope, hash_bytes, hash_json
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import ArtifactStore, LocalArtifactStore
from quant_system.domain.exceptions import QuantSystemError
from quant_system.domain.types import require_aware, utc_now
from quant_system.research.runtime.models import EvidenceRef


def verify_artifact(store: ArtifactStore, artifact: ArtifactEnvelope) -> Any:
    if isinstance(store, LocalArtifactStore) and artifact.uri.startswith("file://"):
        path = Path(unquote(urlparse(artifact.uri).path))
        if path.is_dir():
            if store.hash_dir(str(path)) != artifact.content_hash:
                raise QuantSystemError(f"artifact directory hash mismatch: {artifact.artifact_id}")
            return None
    raw = store.get_bytes(artifact.uri)
    if hash_bytes(raw) != artifact.content_hash:
        raise QuantSystemError(f"artifact content hash mismatch: {artifact.artifact_id}")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        return raw
    if isinstance(payload, dict):
        for response in payload.get("responses", []):
            if isinstance(response, dict) and "body_base64" in response:
                try:
                    digest = hash_bytes(base64.b64decode(response["body_base64"], validate=True))
                except (ValueError, TypeError) as exc:
                    raise QuantSystemError("invalid raw response encoding") from exc
                if digest != response.get("content_hash"):
                    raise QuantSystemError("raw response content hash mismatch")
    return payload


def _time(value: Any) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return require_aware(parsed)
    except (ValueError, TypeError) as exc:
        raise QuantSystemError("artifact availability must be an aware datetime") from exc


def _available_at(artifact: ArtifactEnvelope, body: Any) -> datetime:
    payload = body if isinstance(body, dict) else {}
    values = [
        value
        for value in (artifact.metadata.get("available_at"), payload.get("available_at"))
        if value is not None
    ]
    return max(map(_time, values)) if values else artifact.created_at


def validate_evidence(
    reference: EvidenceRef,
    store: ArtifactStore,
    registry: ArtifactRegistry,
    *,
    knowledge_cutoff: datetime | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> EvidenceRef:
    reference = EvidenceRef.model_validate(reference.model_dump())
    artifact = registry.get(reference.artifact_id)
    if artifact.content_hash != reference.content_hash:
        raise QuantSystemError("evidence reference content hash mismatch")
    if artifact.schema_version != reference.schema_version:
        raise QuantSystemError("evidence reference schema version mismatch")
    if artifact.status.value in {"FAILED", "REJECTED"}:
        raise QuantSystemError("failed artifact cannot be evidence")
    payload = verify_artifact(store, artifact)
    body = payload if isinstance(payload, dict) else {}
    actual_time = _available_at(artifact, body)
    cutoff = require_aware(knowledge_cutoff if knowledge_cutoff is not None else clock())
    if reference.available_at < actual_time:
        raise QuantSystemError("evidence availability predates the actual artifact availability")
    if reference.available_at > cutoff or actual_time > cutoff:
        raise QuantSystemError("evidence is unavailable at the knowledge cutoff")
    kinds = {
        value
        for value in (artifact.metadata.get("evidence_kind"), artifact.metadata.get("kind"), body.get("kind"))
        if isinstance(value, str) and value in {"FACT", "DERIVED", "OPINION"}
    }
    if artifact.artifact_type.value in {
        "RESEARCH_RUN",
        "RESEARCH_CALL",
        "RESEARCH_REPORT",
    } or artifact.artifact_type.value.endswith("_RESEARCH_REPORT"):
        kinds.add("OPINION")
    if kinds and kinds != {reference.kind}:
        raise QuantSystemError("evidence kind cannot be relabelled")
    namespace = artifact.metadata.get("namespace")
    if namespace is not None and namespace != reference.namespace:
        raise QuantSystemError("evidence namespace mismatch")
    parents = set(artifact.parent_artifact_ids)
    raw_id = artifact.data_snapshot_id or body.get("raw_snapshot_id")
    if raw_id and raw_id != artifact.artifact_id:
        if raw_id not in parents:
            raise QuantSystemError("evidence raw snapshot is not bound to its artifact")
        parents.add(raw_id)
    visited = {artifact.artifact_id}
    pending = list(parents)
    while pending:
        parent_id = pending.pop()
        if parent_id in visited:
            continue
        visited.add(parent_id)
        parent = registry.get(parent_id)
        parent_body = verify_artifact(store, parent)
        if parent.status.value in {"FAILED", "REJECTED"}:
            raise QuantSystemError("evidence parent artifact failed its quality checks")
        if _available_at(parent, parent_body) > cutoff:
            raise QuantSystemError("evidence parent is unavailable at the knowledge cutoff")
        pending.extend(parent.parent_artifact_ids)
    return reference.model_copy(deep=True)


class EvidenceManifest:
    def __init__(
        self,
        store: ArtifactStore,
        registry: ArtifactRegistry,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._registry = registry
        self._clock = clock
        self._references: dict[tuple[str, str | None], EvidenceRef] = {}
        self._lock = RLock()

    def add(self, reference: EvidenceRef, *, knowledge_cutoff: datetime | None = None) -> EvidenceRef:
        checked = validate_evidence(
            reference,
            self._store,
            self._registry,
            knowledge_cutoff=knowledge_cutoff,
            clock=self._clock,
        )
        with self._lock:
            key = (checked.artifact_id, checked.record_key)
            previous = self._references.get(key)
            if previous is not None and previous != checked:
                raise QuantSystemError("evidence identity cannot be relabelled within a run")
            self._references[key] = checked
        return checked.model_copy(deep=True)

    @property
    def references(self) -> list[EvidenceRef]:
        with self._lock:
            return [
                item.model_copy(deep=True)
                for item in sorted(
                    self._references.values(), key=lambda item: (item.artifact_id, item.record_key or "")
                )
            ]

    def snapshot(self, knowledge_cutoff: datetime | None) -> dict[str, Any]:
        body = {
            "schema_version": "research-evidence-manifest-v1",
            "knowledge_cutoff": knowledge_cutoff.isoformat() if knowledge_cutoff else None,
            "references": [item.model_dump(mode="json") for item in self.references],
        }
        return {**body, "content_hash": hash_json(body)}
