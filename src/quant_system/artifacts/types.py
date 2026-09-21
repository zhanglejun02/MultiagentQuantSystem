"""Typed artifact constructors for the canonical artifact kinds.

Each helper builds an immutable ArtifactEnvelope with the right type tag and
metadata schema. Content payloads live in the ArtifactStore.
"""

from __future__ import annotations

from typing import Any

from quant_system.artifacts.envelope import ArtifactEnvelope, hash_json
from quant_system.domain.enums import ArtifactType


def _make(
    artifact_type: ArtifactType,
    producer: str,
    *,
    uri: str = "",
    content_hash: str = "",
    parents: tuple[str, ...] = (),
    data_snapshot_id: str | None = None,
    experiment_id: str | None = None,
    code_commit: str = "unknown",
    config: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> ArtifactEnvelope:
    return ArtifactEnvelope(
        artifact_type=artifact_type,
        producer=producer,
        uri=uri,
        content_hash=content_hash,
        parent_artifact_ids=tuple(sorted(set(parents))),
        data_snapshot_id=data_snapshot_id,
        experiment_id=experiment_id,
        code_commit=code_commit,
        config_hash=hash_json(config) if config else "",
        metadata=metadata or {},
    )


def data_snapshot_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    return _make(ArtifactType.DATA_SNAPSHOT, producer, **kw)


def universe_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    return _make(ArtifactType.UNIVERSE, producer, **kw)


def feature_set_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    return _make(ArtifactType.FEATURE_SET, producer, **kw)


def model_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    return _make(ArtifactType.MODEL, producer, **kw)


def signal_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    return _make(ArtifactType.SIGNAL, producer, **kw)


def portfolio_target_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    return _make(ArtifactType.PORTFOLIO_TARGET, producer, **kw)


def backtest_spec_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    return _make(ArtifactType.BACKTEST_SPEC, producer, **kw)


def backtest_result_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    return _make(ArtifactType.BACKTEST_RESULT, producer, **kw)


def futures_backtest_spec_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    return _make(ArtifactType.FUTURES_BACKTEST_SPEC, producer, **kw)


def futures_backtest_result_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    return _make(ArtifactType.FUTURES_BACKTEST_RESULT, producer, **kw)


def validation_report_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    return _make(ArtifactType.VALIDATION_REPORT, producer, **kw)


def strategy_package_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    return _make(ArtifactType.STRATEGY_PACKAGE, producer, **kw)


def deployment_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    return _make(ArtifactType.DEPLOYMENT, producer, **kw)


def deployment_proposal_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    """Agents may create proposals; they may NOT activate deployments."""
    return _make(ArtifactType.DEPLOYMENT_PROPOSAL, producer, **kw)


def research_report_artifact(producer: str, **kw: Any) -> ArtifactEnvelope:
    return _make(ArtifactType.RESEARCH_REPORT, producer, **kw)
