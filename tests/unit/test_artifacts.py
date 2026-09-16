from __future__ import annotations

import pytest
from pydantic import ValidationError

from quant_system.artifacts.envelope import ArtifactEnvelope
from quant_system.artifacts.lineage import ArtifactLineageService
from quant_system.artifacts.types import (
    backtest_result_artifact,
    backtest_spec_artifact,
    data_snapshot_artifact,
)
from quant_system.domain.enums import ArtifactType
from quant_system.domain.exceptions import ArtifactNotFoundError, ImmutabilityError


def test_envelope_is_immutable():
    env = data_snapshot_artifact(producer="t")
    with pytest.raises(ValidationError):
        env.uri = "file:///tmp/x"  # type: ignore[misc]


def test_register_twice_rejected(registry):
    env = data_snapshot_artifact(producer="t")
    registry.register(env)
    with pytest.raises(ImmutabilityError):
        registry.register(env)


def test_missing_parent_rejected(registry):
    env = backtest_spec_artifact(producer="t", parents=("art_nonexistent",))
    with pytest.raises(ArtifactNotFoundError):
        registry.register(env)


def test_lineage_parents_children(registry):
    snap = registry.register(data_snapshot_artifact(producer="t"))
    spec = registry.register(backtest_spec_artifact(producer="t", parents=(snap.artifact_id,)))
    result = registry.register(
        backtest_result_artifact(producer="t", parents=(snap.artifact_id, spec.artifact_id))
    )

    assert {p.artifact_id for p in registry.parents(result.artifact_id)} == {
        snap.artifact_id, spec.artifact_id,
    }
    assert {c.artifact_id for c in registry.children(snap.artifact_id)} == {
        spec.artifact_id, result.artifact_id,
    }

    lineage = ArtifactLineageService(registry)
    graph = lineage.lineage(result.artifact_id)
    assert {a.artifact_id for a in graph["ancestors"]} == {snap.artifact_id, spec.artifact_id}
    assert graph["descendants"] == []
    down = lineage.lineage(snap.artifact_id)["descendants"]
    assert {d.artifact_id for d in down} == {spec.artifact_id, result.artifact_id}
    assert lineage.verify_lineage(result.artifact_id)


def test_find_by_type_and_experiment(registry):
    registry.register(data_snapshot_artifact(producer="t"))
    registry.register(backtest_spec_artifact(producer="t", experiment_id="exp_1"))
    assert len(registry.find_by_type(ArtifactType.DATA_SNAPSHOT)) == 1
    assert len(registry.find_by_experiment("exp_1")) == 1


def test_derive_creates_new_artifact_with_lineage():
    env = data_snapshot_artifact(producer="t")
    child = env.derive(metadata={"reason": "reprocessed"})
    assert child.artifact_id != env.artifact_id
    assert env.artifact_id in child.parent_artifact_ids


def test_naive_created_at_rejected():
    from datetime import datetime

    with pytest.raises(ValidationError):
        ArtifactEnvelope(
            artifact_type=ArtifactType.DATA_SNAPSHOT,
            producer="t",
            created_at=datetime(2024, 1, 1),  # naive
        )
