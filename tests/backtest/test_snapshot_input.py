from __future__ import annotations

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from quant_system.backtest.engine import EventDrivenBacktestEngine
from quant_system.backtest.request import BacktestRequest
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.data.snapshots.service import DataSnapshotService
from quant_system.domain.exceptions import QuantSystemError
from quant_system.fixtures import generate_fixture_data
from quant_system.strategies import resolve_strategy


def test_backtest_consumes_frozen_snapshot_not_mutable_source(
    tmp_path, fixture_dir, rulebook, store, registry
):
    source = tmp_path / "mutable"
    shutil.copytree(fixture_dir, source)
    snapshots = DataSnapshotService(store, registry)
    snapshot = snapshots.create_snapshot(str(source), producer="test")
    engine = EventDrivenBacktestEngine(
        provider=LocalParquetProvider(str(source)),
        rulebook=rulebook,
        store=store,
        registry=registry,
        strategy_resolver=resolve_strategy,
    )
    request = BacktestRequest(
        strategy_id="momentum_topk",
        dataset_snapshot_id=snapshot.artifact_id,
        start_date=date(2024, 2, 1),
        end_date=date(2024, 3, 29),
        initial_cash=Decimal("1000000"),
    )
    first = engine.run(request)
    generate_fixture_data(str(source), seed=43)
    second = engine.run(request)
    assert first.performance_metrics == second.performance_metrics
    assert first.execution_metrics == second.execution_metrics
    assert snapshots.verify_snapshot(snapshot)

    frozen = Path(snapshot.uri.removeprefix("file://"))
    (frozen / "unexpected.txt").write_text("corruption")
    with pytest.raises(QuantSystemError, match="integrity"):
        engine.run(request)
