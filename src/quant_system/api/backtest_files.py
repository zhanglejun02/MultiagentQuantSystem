"""Read access to persisted backtest run directories.

Routers never touch Path/json for backtest artifacts; this reader resolves the
artifact, verifies its type and decodes the run files.
"""

from __future__ import annotations

import json
from pathlib import Path

from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.domain.enums import ArtifactType
from quant_system.domain.exceptions import ArtifactNotFoundError, QuantSystemError


class WrongArtifactTypeError(QuantSystemError):
    """Requested artifact exists but is not of the expected type."""


def run_dir(uri: str) -> Path:
    if not uri.startswith("file://"):
        raise ArtifactNotFoundError(f"unsupported artifact uri {uri}")
    path = Path(uri[len("file://") :])
    if not path.exists():
        raise ArtifactNotFoundError(f"artifact content missing at {path}")
    return path


def read_jsonl(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open() as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
            if len(out) >= limit:
                break
    return out


class BacktestResultReader:
    def __init__(self, registry: ArtifactRegistry, artifact_type: ArtifactType, label: str) -> None:
        self._registry = registry
        self._artifact_type = artifact_type
        self._label = label

    def directory(self, artifact_id: str) -> Path:
        envelope = self._registry.get(artifact_id)
        if envelope.artifact_type != self._artifact_type:
            raise WrongArtifactTypeError(f"{artifact_id} is not a {self._label}")
        return run_dir(envelope.uri)

    def metrics(self, artifact_id: str) -> dict:
        return json.loads((self.directory(artifact_id) / "metrics.json").read_text())

    def equity_curve(self, artifact_id: str) -> list[dict]:
        lines = (self.directory(artifact_id) / "equity_curve.csv").read_text().strip().splitlines()
        header = lines[0].split(",")
        return [dict(zip(header, line.split(","), strict=True)) for line in lines[1:]]

    def orders(self, artifact_id: str, limit: int) -> list[dict]:
        return read_jsonl(self.directory(artifact_id) / "orders.jsonl", limit)

    def trades(self, artifact_id: str, limit: int) -> list[dict]:
        return read_jsonl(self.directory(artifact_id) / "trades.jsonl", limit)

    def events(self, artifact_id: str, event_type: str | None, limit: int) -> list[dict]:
        rows = read_jsonl(self.directory(artifact_id) / "events.jsonl", 100_000)
        if event_type:
            rows = [row for row in rows if row.get("event_type") == event_type]
        return rows[:limit]

    def positions(self, artifact_id: str) -> dict:
        return json.loads((self.directory(artifact_id) / "positions.json").read_text())

    def latest_positions(self) -> dict:
        envelopes = self._registry.find_by_type(self._artifact_type)
        if not envelopes:
            raise ArtifactNotFoundError(f"no {self._label}s")
        latest = max(envelopes, key=lambda envelope: envelope.created_at)
        doc = json.loads((run_dir(latest.uri) / "positions.json").read_text())
        doc["source_artifact_id"] = latest.artifact_id
        return doc
