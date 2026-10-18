"""Prepare a real frozen dataset or run a strictly offline baseline experiment."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from quant_system.artifacts.envelope import hash_bytes, hash_json
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.data.ingestion.baseline import BaselineDatasetBuilder
from quant_system.data.ingestion.tushare import TushareClient
from quant_system.data.snapshots.service import DataSnapshotService
from quant_system.domain.enums import ArtifactType
from quant_system.infra.db import create_all, make_engine, make_session_factory
from quant_system.research.baseline.runner import BaselineRunner
from quant_system.research.baseline.spec import BaselineSpec
from quant_system.research.experiments import ExperimentService

ROOT = Path(__file__).resolve().parents[1]


def code_identity() -> tuple[str, str, list[Path]]:
    paths = sorted((ROOT / "src" / "quant_system").rglob("*.py"))
    paths += [
        ROOT / "scripts" / "run_baseline_research.py",
        ROOT / "configs" / "markets" / "cn_a.yaml",
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
    ]
    digest = hash_json({str(path.relative_to(ROOT)): hash_bytes(path.read_bytes()) for path in paths})
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    return commit, digest, paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument(
        "--config", type=Path, default=ROOT / "configs" / "research" / "a_share_baseline.yaml"
    )
    parser.add_argument("--workspace", type=Path, default=ROOT / "var" / "research" / "a_share_baseline")
    parser.add_argument("--snapshot-id", default=None)
    args = parser.parse_args()
    spec = BaselineSpec.load(args.config)
    protocol_hash = hash_json(spec.model_dump(mode="json"))
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    database = make_engine(f"sqlite:///{workspace / 'registry.db'}")
    create_all(database)
    sessions = make_session_factory(database)
    registry = ArtifactRegistry(sessions)
    store = LocalArtifactStore(str(workspace / "artifacts"))
    snapshots = DataSnapshotService(store, registry)
    commit, code_hash, code_paths = code_identity()
    if args.action == "prepare":
        existing = [
            item
            for item in registry.find_by_type(ArtifactType.DATA_SNAPSHOT)
            if item.metadata.get("protocol_hash") == protocol_hash
            and (Path(item.uri.removeprefix("file://")) / "raw").is_dir()
        ]
        if existing:
            snapshot = max(existing, key=lambda item: item.created_at)
            snapshots.resolve_path(snapshot.artifact_id)
        else:
            prepared = workspace / "prepared" / protocol_hash
            if prepared.exists() and (prepared / "raw").is_dir():
                metadata = json.loads((prepared / "dataset_metadata.json").read_text())
                if metadata["protocol_hash"] != protocol_hash:
                    raise ValueError("prepared dataset protocol mismatch")
            else:
                if prepared.exists():
                    prepared = workspace / "prepared" / f"{protocol_hash}-with-raw"
                metadata = BaselineDatasetBuilder(TushareClient(), workspace / "raw").prepare(
                    prepared,
                    symbols=spec.symbols,
                    benchmark=spec.benchmark,
                    start=spec.data_start,
                    end=spec.data_end,
                    protocol_hash=protocol_hash,
                    benchmark_source=spec.benchmark_source,
                    reference_source=spec.reference_source,
                    adjustment_source=spec.adjustment_source,
                    dividend_source=spec.dividend_source,
                )
            snapshot = snapshots.create_snapshot(
                str(prepared), producer="baseline-dataset-preparation", code_commit=commit, metadata=metadata
            )
            snapshots.resolve_path(snapshot.artifact_id)
        print(
            json.dumps(
                {
                    "snapshot_id": snapshot.artifact_id,
                    "data_hash": snapshot.content_hash,
                    "protocol_hash": protocol_hash,
                    "frozen_uri": snapshot.uri,
                },
                indent=2,
            )
        )
        return
    if not args.snapshot_id:
        parser.error("offline run requires an explicit --snapshot-id printed by prepare")
    environment = {
        "python": platform.python_version(),
        **{
            package: importlib.metadata.version(package)
            for package in ("pydantic", "pyarrow", "SQLAlchemy", "PyYAML")
        },
    }
    with tempfile.TemporaryDirectory() as temporary:
        bundle = Path(temporary)
        for path in code_paths:
            destination = bundle / path.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        (bundle / "baseline_protocol.json").write_text(json.dumps(spec.model_dump(mode="json"), indent=2))
        report = BaselineRunner(
            store=store,
            registry=registry,
            experiments=ExperimentService(sessions),
            rulebook_path=ROOT / "configs" / "markets" / "cn_a.yaml",
            code_commit=commit,
            code_hash=code_hash,
            environment=environment,
            code_bundle_path=bundle,
        ).run(spec, args.snapshot_id)
    print(
        json.dumps(
            {
                "report_artifact_id": report.artifact_id,
                "report_uri": report.uri,
                "assessment": report.metadata["assessment"],
                "deployment_eligible": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
