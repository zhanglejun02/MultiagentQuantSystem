"""ArtifactStore port + local filesystem adapter.

Content lives outside the DB; the registry stores metadata + content hash.
Replaceable later by S3/MinIO without touching domain code.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Protocol

from quant_system.artifacts.envelope import hash_bytes
from quant_system.domain.exceptions import ArtifactNotFoundError, ImmutabilityError


class ArtifactStore(Protocol):
    def put_bytes(self, key: str, data: bytes) -> tuple[str, str]:
        """Returns (uri, content_hash)."""
        ...

    def put_json(self, key: str, obj: Any) -> tuple[str, str]: ...

    def put_dir(self, key: str, src_dir: str) -> tuple[str, str]: ...

    def put_content_addressed_json(self, namespace: str, obj: Any) -> tuple[str, str]: ...

    def put_content_addressed_dir(self, namespace: str, src_dir: str) -> tuple[str, str]: ...

    def get_bytes(self, uri: str) -> bytes: ...

    def get_json(self, uri: str) -> Any: ...

    def exists(self, uri: str) -> bool: ...


class LocalArtifactStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError(f"key escapes store root: {key}")
        return p

    def put_bytes(self, key: str, data: bytes) -> tuple[str, str]:
        path = self._path(key)
        if path.exists():
            raise ImmutabilityError(f"artifact content already exists at {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"file://{path}", hash_bytes(data)

    def put_json(self, key: str, obj: Any) -> tuple[str, str]:
        data = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False, indent=1).encode()
        return self.put_bytes(key, data)

    def put_content_addressed_json(self, namespace: str, obj: Any) -> tuple[str, str]:
        data = json.dumps(
            obj, sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":")
        ).encode()
        digest = hash_bytes(data)
        path = self._path(f"{namespace}/{digest}.json")
        if path.exists():
            if hash_bytes(path.read_bytes()) != digest:
                raise ImmutabilityError(f"content-addressed file drifted at {path}")
            return f"file://{path}", digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"file://{path}", digest

    def put_dir(self, key: str, src_dir: str) -> tuple[str, str]:
        """Copy a directory into the store; hash is over the sorted file manifest."""
        dest = self._path(key)
        if dest.exists():
            raise ImmutabilityError(f"artifact content already exists at {dest}")
        shutil.copytree(src_dir, dest)
        digest = self.hash_dir(str(dest))
        return f"file://{dest}", digest

    def put_content_addressed_dir(self, namespace: str, src_dir: str) -> tuple[str, str]:
        """Store a directory under its digest, reusing identical immutable content."""
        digest = self.hash_dir(src_dir)
        dest = self._path(f"{namespace}/{digest}")
        if dest.exists():
            if self.hash_dir(str(dest)) != digest:
                raise ImmutabilityError(f"content-addressed directory drifted at {dest}")
            return f"file://{dest}", digest
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_dir, dest)
        return f"file://{dest}", digest

    @staticmethod
    def hash_dir(directory: str) -> str:
        entries = []
        for f in sorted(Path(directory).rglob("*")):
            if f.is_file():
                entries.append(f"{f.relative_to(directory)}:{hash_bytes(f.read_bytes())}")
        return hash_bytes("\n".join(entries).encode())

    @staticmethod
    def _uri_to_path(uri: str) -> Path:
        if not uri.startswith("file://"):
            raise ArtifactNotFoundError(f"unsupported uri scheme: {uri}")
        return Path(uri[len("file://"):])

    def get_bytes(self, uri: str) -> bytes:
        path = self._uri_to_path(uri)
        if not path.exists():
            raise ArtifactNotFoundError(uri)
        return path.read_bytes()

    def get_json(self, uri: str) -> Any:
        return json.loads(self.get_bytes(uri).decode())

    def exists(self, uri: str) -> bool:
        try:
            return self._uri_to_path(uri).exists()
        except ArtifactNotFoundError:
            return False
