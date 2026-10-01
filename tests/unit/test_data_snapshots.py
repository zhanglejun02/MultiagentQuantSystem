from __future__ import annotations

from pathlib import Path

from quant_system.artifacts.store import LocalArtifactStore
from quant_system.data.snapshots.service import DataSnapshotService


def test_snapshot_is_immutable_after_source_changes(tmp_path: Path, registry) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "bars.parquet").write_bytes(b"version-1")
    service = DataSnapshotService(LocalArtifactStore(str(tmp_path / "artifacts")), registry)

    snapshot = service.create_snapshot(str(source), producer="test")
    (source / "bars.parquet").write_bytes(b"version-2")

    assert snapshot.uri != f"file://{source.resolve()}"
    assert service.verify_snapshot(snapshot)


def test_snapshot_reuses_content_addressed_directory(tmp_path: Path, registry) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "bars.parquet").write_bytes(b"same-content")
    service = DataSnapshotService(LocalArtifactStore(str(tmp_path / "artifacts")), registry)

    first = service.create_snapshot(str(source), producer="test")
    second = service.create_snapshot(str(source), producer="test")

    assert first.artifact_id != second.artifact_id
    assert first.content_hash == second.content_hash
    assert first.uri == second.uri
    assert service.verify_snapshot(first)
    assert service.verify_snapshot(second)