"""Quality-gated publication of immutable daily market artifacts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from quant_system.artifacts.envelope import ArtifactEnvelope
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.artifacts.types import (
    data_quality_report_artifact,
    market_snapshot_artifact,
)
from quant_system.data.market_snapshot import build_market_snapshot
from quant_system.data.quality import MarketDataQualityGate
from quant_system.data.snapshots.service import DataSnapshotService
from quant_system.domain.enums import ArtifactStatus, ArtifactType


@dataclass(frozen=True)
class DailyMarketPipelineResult:
    passed: bool
    quality_report: ArtifactEnvelope
    data_snapshot: ArtifactEnvelope | None = None
    market_snapshot: ArtifactEnvelope | None = None


class DailyMarketPipeline:
    def __init__(self, store: LocalArtifactStore, registry: ArtifactRegistry) -> None:
        self._store = store
        self._registry = registry
        self._snapshots = DataSnapshotService(store, registry)
        self._quality = MarketDataQualityGate()

    def publish(
        self,
        *,
        trade_date: date,
        data_dir: str | Path,
        datasets: Mapping[str, Iterable[Mapping[str, Any]]],
        required_datasets: Iterable[str],
        optional_datasets: Iterable[str] = (),
        acquisition_errors: Iterable[str] = (),
        expected_equity_symbols: int | None = None,
        minimum_equity_coverage: float = 0.95,
        producer: str = "daily-market-pipeline",
        code_commit: str = "unknown",
    ) -> DailyMarketPipelineResult:
        materialized = {name: list(rows) for name, rows in datasets.items()}
        report = self._quality.evaluate(
            datasets=materialized,
            required_datasets=required_datasets,
            optional_datasets=optional_datasets,
            acquisition_errors=acquisition_errors,
            expected_equity_symbols=expected_equity_symbols,
            minimum_equity_coverage=minimum_equity_coverage,
        )
        report_payload = {
            "schema_version": "1.0",
            "trade_date": trade_date.isoformat(),
            **report.model_dump(mode="json"),
        }
        quality_uri, quality_hash = self._store.put_content_addressed_json(
            "data_quality_reports", report_payload
        )
        quality_status = ArtifactStatus.VERIFIED if report.passed else ArtifactStatus.FAILED
        quality_artifact = self._find_existing(
            ArtifactType.DATA_QUALITY_REPORT,
            quality_hash,
            trade_date,
            status=quality_status,
        )
        if quality_artifact is None:
            quality_artifact = self._registry.register(data_quality_report_artifact(
                producer=producer,
                uri=quality_uri,
                content_hash=quality_hash,
                code_commit=code_commit,
                metadata={
                    "trade_date": trade_date.isoformat(),
                    "statistics": report.statistics,
                    "issue_count": len(report.issues),
                },
                status=quality_status,
            ))
        if not report.passed:
            return DailyMarketPipelineResult(False, quality_artifact)

        data_hash = self._store.hash_dir(str(data_dir))
        data_snapshot = self._find_existing(
            ArtifactType.DATA_SNAPSHOT,
            data_hash,
            trade_date,
            parents=(quality_artifact.artifact_id,),
        )
        if data_snapshot is None:
            data_snapshot = self._snapshots.create_snapshot(
                str(data_dir),
                producer=producer,
                code_commit=code_commit,
                parents=(quality_artifact.artifact_id,),
                metadata={"trade_date": trade_date.isoformat()},
            )
        snapshot_payload = build_market_snapshot(trade_date, materialized)
        market_uri, market_hash = self._store.put_content_addressed_json(
            "market_snapshots", snapshot_payload
        )
        market_snapshot = self._find_existing(
            ArtifactType.MARKET_SNAPSHOT,
            market_hash,
            trade_date,
            parents=(data_snapshot.artifact_id,),
        )
        if market_snapshot is None:
            market_snapshot = self._registry.register(market_snapshot_artifact(
                producer=producer,
                uri=market_uri,
                content_hash=market_hash,
                parents=(data_snapshot.artifact_id,),
                data_snapshot_id=data_snapshot.artifact_id,
                code_commit=code_commit,
                metadata={"trade_date": trade_date.isoformat()},
                status=ArtifactStatus.VERIFIED,
            ))
        return DailyMarketPipelineResult(
            True,
            quality_artifact,
            data_snapshot,
            market_snapshot,
        )

    def read_market_snapshot(self, artifact: ArtifactEnvelope) -> dict[str, Any]:
        payload = self._store.get_json(artifact.uri)
        if not isinstance(payload, dict):
            raise ValueError("market snapshot payload must be a JSON object")
        return payload

    def _find_existing(
        self,
        artifact_type: ArtifactType,
        content_hash: str,
        trade_date: date,
        *,
        status: ArtifactStatus | None = None,
        parents: tuple[str, ...] | None = None,
    ) -> ArtifactEnvelope | None:
        for artifact in self._registry.find_by_type(artifact_type):
            if artifact.content_hash != content_hash:
                continue
            if artifact.metadata.get("trade_date") != trade_date.isoformat():
                continue
            if status is not None and artifact.status != status:
                continue
            if parents is not None and artifact.parent_artifact_ids != tuple(sorted(parents)):
                continue
            return artifact
        return None