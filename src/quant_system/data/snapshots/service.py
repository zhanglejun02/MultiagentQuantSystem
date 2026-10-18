"""DataSnapshot: content-addressed freeze of a data directory.

A snapshot pins the exact files a backtest/experiment reads, making runs
reproducible and lineage traceable.
"""

from __future__ import annotations

from pathlib import Path

from quant_system.artifacts.envelope import ArtifactEnvelope, hash_bytes
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.artifacts.types import data_snapshot_artifact
from quant_system.domain.enums import ArtifactType
from quant_system.domain.exceptions import QuantSystemError


class DataSnapshotService:
    def __init__(self, store: LocalArtifactStore, registry: ArtifactRegistry) -> None:
        self._store = store
        self._registry = registry

    def create_snapshot(
        self,
        data_dir: str,
        *,
        producer: str,
        code_commit: str = "unknown",
        parents: tuple[str, ...] = (),
        metadata: dict[str, object] | None = None,
    ) -> ArtifactEnvelope:
        manifest = self._manifest(data_dir)
        content_hash = hash_bytes(
            "\n".join(f"{name}:{digest}" for name, digest in sorted(manifest.items())).encode()
        )
        uri, stored_hash = self._store.put_content_addressed_dir("data_snapshots", data_dir)
        if stored_hash != content_hash:
            raise ValueError("snapshot manifest hash does not match stored directory hash")
        env = data_snapshot_artifact(
            producer=producer,
            uri=uri,
            content_hash=content_hash,
            code_commit=code_commit,
            parents=parents,
            metadata={"files": manifest, "n_files": len(manifest), **(metadata or {})},
        )
        return self._registry.register(env)

    @staticmethod
    def _manifest(data_dir: str) -> dict[str, str]:
        root = Path(data_dir)
        out: dict[str, str] = {}
        for f in sorted(root.rglob("*")):
            if f.is_file():
                out[str(f.relative_to(root))] = hash_bytes(f.read_bytes())
        return out

    def verify_snapshot(self, snapshot: ArtifactEnvelope) -> bool:
        """True iff the on-disk data still matches the frozen manifest."""
        data_dir = snapshot.uri[len("file://"):]
        return self._manifest(data_dir) == snapshot.metadata.get("files", {})

    def resolve_path(self, snapshot_id: str) -> Path:
        snapshot = self._registry.get(snapshot_id)
        if snapshot.artifact_type != ArtifactType.DATA_SNAPSHOT:
            raise QuantSystemError("backtest input must be a DATA_SNAPSHOT")
        if not snapshot.uri.startswith("file://"):
            raise QuantSystemError("only local frozen data snapshots are supported")
        path = Path(snapshot.uri.removeprefix("file://"))
        if not path.is_dir() or not snapshot.metadata.get("files"):
            raise QuantSystemError("data snapshot is missing or empty")
        if not self.verify_snapshot(snapshot) or self._store.hash_dir(str(path)) != snapshot.content_hash:
            raise QuantSystemError("data snapshot integrity check failed")
        return path
