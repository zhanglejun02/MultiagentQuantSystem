from pathlib import Path

from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.domain.enums import ArtifactType
from quant_system.futures.backtest import FuturesBacktestEngine, FuturesBacktestRequest
from quant_system.futures.contracts import FuturesContractCatalog
from quant_system.futures.data import FixtureFuturesData
from quant_system.infra.db import create_all, make_engine, make_session_factory


def _engine(root: Path):
    root.mkdir()
    database = make_engine(f"sqlite:///{root / 'registry.db'}")
    create_all(database)
    registry = ArtifactRegistry(make_session_factory(database))
    engine = FuturesBacktestEngine(
        catalog=FuturesContractCatalog.from_yaml("configs/futures/contracts.yaml"),
        data=FixtureFuturesData(),
        store=LocalArtifactStore(str(root / "artifacts")),
        registry=registry,
    )
    return engine, registry


def test_futures_backtest_is_deterministic_and_has_distinct_artifact_type(tmp_path: Path) -> None:
    engine, registry = _engine(tmp_path / "runs")
    first = engine.run(FuturesBacktestRequest())
    second = engine.run(FuturesBacktestRequest())

    assert first.run_id != second.run_id
    assert first.run_dir != second.run_dir
    assert first.final_event_hash == second.final_event_hash
    assert first.metrics == second.metrics
    assert first.metrics["trade_count"] > 0
    assert Path(first.run_dir, "events.jsonl").exists()
    assert Path(second.run_dir, "events.jsonl").exists()
    assert registry.get(first.result_artifact_id).artifact_type == (
        ArtifactType.FUTURES_BACKTEST_RESULT
    )