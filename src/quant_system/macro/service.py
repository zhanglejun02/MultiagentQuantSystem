"""Immutable raw -> macro snapshot collection and verified deterministic analysis."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from quant_system.artifacts.envelope import ArtifactEnvelope, hash_bytes
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import ArtifactStore
from quant_system.domain.enums import ArtifactStatus, ArtifactType
from quant_system.domain.types import require_aware, utc_now
from quant_system.macro.analysis import analyze_observations, series_coverage
from quant_system.macro.ingestion import (
    FRED_OBSERVATIONS_URL,
    MacroSourceConfig,
    MacroTransport,
    UrllibMacroTransport,
    collect_sources,
    expanded_series,
)
from quant_system.macro.models import (
    MacroCollection,
    MacroCollectRequest,
    MacroQualityError,
    MacroSnapshot,
)


def _macro_snapshot_type() -> ArtifactType:
    try:
        return ArtifactType("MACRO_SNAPSHOT")
    except ValueError:
        raise RuntimeError("macro requires the shared ArtifactType.MACRO_SNAPSHOT enum member") from None


class MacroService:
    def __init__(
        self,
        store: ArtifactStore,
        registry: ArtifactRegistry,
        *,
        clock: Callable[[], datetime] = utc_now,
        transport: MacroTransport | None = None,
        config: MacroSourceConfig | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._clock = clock
        self._config = config or MacroSourceConfig()
        self._transport = (
            transport
            if transport is not None
            else UrllibMacroTransport(
                max_response_bytes=self._config.max_response_bytes,
            )
        )

    def _save(
        self,
        payload: dict[str, Any],
        *,
        artifact_type: ArtifactType,
        as_of: datetime,
        parents: tuple[str, ...] = (),
        raw_snapshot_id: str | None = None,
        failed: bool = False,
    ) -> ArtifactEnvelope:
        namespace = "macro/raw" if artifact_type == ArtifactType.DATA_SNAPSHOT else "macro/snapshots"
        uri, digest = self._store.put_content_addressed_json(namespace, payload)
        return self._registry.register(
            ArtifactEnvelope(
                artifact_type=artifact_type,
                schema_version=payload["schema_version"],
                created_at=as_of,
                producer="macro.collect",
                parent_artifact_ids=parents,
                data_snapshot_id=raw_snapshot_id,
                uri=uri,
                content_hash=digest,
                status=ArtifactStatus.FAILED if failed else ArtifactStatus.VERIFIED,
                metadata={
                    "domain": "macro",
                    "as_of": as_of.isoformat(),
                    "available_at": payload.get("available_at", as_of.isoformat()),
                    "historical_pit_certified": False,
                },
            )
        )

    def collect(self, request: MacroCollectRequest) -> MacroCollection:
        snapshot_type = _macro_snapshot_type()
        started_at = require_aware(self._clock()).astimezone(UTC)
        end_date = request.end_date or started_at.date()
        if end_date > started_at.date() or request.start_date > end_date:
            raise ValueError("collection window must end no later than the actual collection date")
        batch = collect_sources(
            request,
            end_date=end_date,
            transport=self._transport,
            clock=self._clock,
            config=self._config,
        )
        as_of = require_aware(self._clock()).astimezone(UTC)
        if as_of < started_at or any(row.available_at > as_of for row in batch.observations):
            raise ValueError("macro collection clock moved backwards")
        available_at = max((row.available_at for row in batch.observations), default=as_of)
        quality_errors = [f"{key}: {reason}" for key, reason in sorted(batch.missing.items())]
        raw = self._save(
            {
                "schema_version": "macro-raw-v1",
                "domain": "macro",
                "as_of": as_of.isoformat(),
                "available_at": available_at.isoformat(),
                "request": request.model_dump(mode="json"),
                "resolved_end_date": end_date.isoformat(),
                "responses": batch.responses,
                "missing": batch.missing,
                "quality_errors": quality_errors,
            },
            artifact_type=ArtifactType.DATA_SNAPSHOT,
            as_of=as_of,
            failed=not batch.observations,
        )
        if not batch.observations:
            raise MacroQualityError(
                quality_errors or ["NO_USABLE_OBSERVATIONS"], raw_snapshot_id=raw.artifact_id
            )
        snapshot = MacroSnapshot(
            raw_snapshot_id=raw.artifact_id,
            as_of=as_of,
            available_at=available_at,
            request=request,
            resolved_end_date=end_date,
            series_ids=expanded_series(request.series_ids),
            observations=batch.observations,
            missing=batch.missing,
            quality_errors=quality_errors,
        )
        artifact = self._save(
            snapshot.model_dump(mode="json"),
            artifact_type=snapshot_type,
            as_of=as_of,
            parents=(raw.artifact_id,),
            raw_snapshot_id=raw.artifact_id,
        )
        coverage = series_coverage(snapshot.observations, series_ids=snapshot.series_ids, as_of=as_of)
        return MacroCollection(
            snapshot_id=artifact.artifact_id,
            raw_snapshot_id=raw.artifact_id,
            as_of=as_of,
            available_at=available_at,
            coverage=coverage,
            missing=batch.missing,
            quality_errors=quality_errors,
            summary={
                "status": "PARTIAL" if batch.missing else "AVAILABLE",
                "observation_count": len(batch.observations),
                "series_with_data": sum(item.count > 0 for item in coverage.values()),
                "series_requested": snapshot.series_ids,
                "requested_start_date": request.start_date.isoformat(),
                "resolved_end_date": end_date.isoformat(),
                "historical_pit_certified": False,
                "historical_coverage_certified": False,
                "coverage_note": "Counts are actual observations, not a guarantee of complete historical coverage.",
            },
        )

    def _read_verified(self, artifact: ArtifactEnvelope) -> dict[str, Any]:
        body = self._store.get_bytes(artifact.uri)
        if hash_bytes(body) != artifact.content_hash:
            raise ValueError("macro artifact content hash mismatch")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("macro artifact payload must be an object")
        return payload

    def analyze(
        self,
        snapshot_id: str,
        *,
        knowledge_cutoff: datetime | None = None,
    ) -> dict[str, Any]:
        if knowledge_cutoff is not None:
            require_aware(knowledge_cutoff)
        artifact = self._registry.get(snapshot_id)
        if artifact.artifact_type != _macro_snapshot_type() or artifact.status != ArtifactStatus.VERIFIED:
            raise ValueError("a verified MACRO_SNAPSHOT is required")
        snapshot = MacroSnapshot.model_validate(self._read_verified(artifact))
        if (
            snapshot.raw_snapshot_id not in artifact.parent_artifact_ids
            or artifact.data_snapshot_id != snapshot.raw_snapshot_id
        ):
            raise ValueError("macro snapshot raw parent binding mismatch")
        raw_artifact = self._registry.get(snapshot.raw_snapshot_id)
        if (
            raw_artifact.artifact_type != ArtifactType.DATA_SNAPSHOT
            or raw_artifact.status != ArtifactStatus.VERIFIED
        ):
            raise ValueError("macro snapshot requires a verified raw DATA_SNAPSHOT")
        raw = self._read_verified(raw_artifact)
        if raw.get("schema_version") != "macro-raw-v1" or raw.get("domain") != "macro":
            raise ValueError("macro raw snapshot schema mismatch")
        if (
            raw.get("request") != snapshot.request.model_dump(mode="json")
            or raw.get("resolved_end_date") != snapshot.resolved_end_date.isoformat()
            or snapshot.series_ids != expanded_series(snapshot.request.series_ids)
        ):
            raise ValueError("macro snapshot request binding mismatch")
        response_facts = set()
        for response in raw["responses"]:
            if "body_base64" not in response:
                continue
            digest = hash_bytes(base64.b64decode(response["body_base64"], validate=True))
            if digest != response["content_hash"]:
                raise ValueError("macro raw response hash mismatch")
            fetched_at = require_aware(datetime.fromisoformat(response["ingested_at"]))
            response_facts.add(
                (
                    digest,
                    response["source_url"],
                    response["request_parameters"].get("series_id"),
                    fetched_at,
                )
            )
        seen = set()
        for row in snapshot.observations:
            key = row.series_id, row.observation_date
            response_binding = (
                row.raw_content_hash,
                FRED_OBSERVATIONS_URL if row.source == "FRED" else row.source_url,
                row.series_id if row.source == "FRED" else None,
                row.ingested_at,
            )
            if (
                key in seen
                or row.series_id not in snapshot.series_ids
                or response_binding not in response_facts
            ):
                raise ValueError("macro observation provenance or uniqueness mismatch")
            if not snapshot.request.start_date <= row.observation_date <= snapshot.resolved_end_date:
                raise ValueError("macro observation outside snapshot window")
            if row.available_at != row.ingested_at or row.published_at is not None:
                raise ValueError("macro current-download availability policy mismatch")
            if row.available_at > snapshot.as_of or row.available_at > snapshot.available_at:
                raise ValueError("macro observation availability exceeds snapshot bounds")
            seen.add(key)
        return analyze_observations(
            snapshot.observations,
            series_ids=snapshot.series_ids,
            snapshot_id=snapshot_id,
            raw_snapshot_id=snapshot.raw_snapshot_id,
            content_hash=artifact.content_hash,
            as_of=snapshot.as_of,
            available_at=snapshot.available_at,
            missing=snapshot.missing,
            knowledge_cutoff=knowledge_cutoff,
        )
