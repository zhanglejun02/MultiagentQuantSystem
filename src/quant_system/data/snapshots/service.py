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


class DataSnapshotService:
    def __init__(self, store: LocalArtifactStore, registry: ArtifactRegistry) -> None:
        self._store = store
        self._registry = registry

    def create_snapshot(self, data_dir: str, *, producer: str, code_commit: str = "unknown") -> ArtifactEnvelope:
        manifest = self._manifest(data_dir)
        content_hash = hash_bytes(
            "\n".join(f"{name}:{digest}" for name, digest in sorted(manifest.items())).encode()
        )
        env = data_snapshot_artifact(
            producer=producer,
            uri=f"file://{Path(data_dir).resolve()}",
            content_hash=content_hash,
            code_commit=code_commit,
            metadata={"files": manifest, "n_files": len(manifest)},
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
