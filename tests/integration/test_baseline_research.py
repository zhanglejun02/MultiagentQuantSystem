import json
import shutil
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quant_system.artifacts.envelope import hash_json
from quant_system.data.snapshots.service import DataSnapshotService
from quant_system.domain.enums import ArtifactType, ExperimentState
from quant_system.domain.exceptions import QuantSystemError
from quant_system.domain.types import bar_available_at
from quant_system.research.baseline.runner import BaselineRunner
from quant_system.research.baseline.spec import BaselineSpec
from quant_system.research.experiments import ExperimentService

ROOT = Path(__file__).resolve().parents[2]


def synthetic_baseline(tmp_path, fixture_dir, store, registry):
    spec = BaselineSpec.model_validate(
        {
            "name": "synthetic_contract_test",
            "data_start": "2024-01-02",
            "data_end": "2024-04-16",
            "symbols": ["600000.SH", "600001.SH", "600002.SH"],
            "benchmark": "000300.SH",
            "lookback_days": 10,
            "segments": [
                {"name": "development", "start": "2024-03-01", "end": "2024-03-15"},
                {"name": "validation", "start": "2024-03-18", "end": "2024-04-01"},
                {"name": "holdout", "start": "2024-04-02", "end": "2024-04-16"},
            ],
            "limitations": ["Synthetic test fixture only; no real-market claim."],
        }
    )
    source = tmp_path / "prepared"
    shutil.copytree(fixture_dir, source)
    symbols = {value.replace(".SH", ".SSE") for value in spec.symbols}
    bars = [
        row
        for row in pq.read_table(source / "bars.parquet").to_pylist()
        if row["symbol"] in symbols and spec.data_start <= row["trade_date"] <= spec.data_end
    ]
    pq.write_table(pa.Table.from_pylist(bars), source / "bars.parquet")
    master = json.loads((source / "securities.json").read_text())
    (source / "securities.json").write_text(json.dumps([row for row in master if row["symbol"] in symbols]))
    (source / "status_intervals.json").write_text("[]")
    (source / "corporate_actions.json").write_text("[]")
    factors = [
        {
            "symbol": row["symbol"],
            "trade_date": row["trade_date"],
            "factor": 1.0,
            "available_at": row["available_at"],
        }
        for row in bars
    ]
    pq.write_table(pa.Table.from_pylist(factors), source / "adjustments.parquet")
    days = sorted({row["trade_date"] for row in bars})
    benchmark = [
        {
            "symbol": spec.benchmark,
            "trade_date": day,
            "close": 100 + index + 1,
            "prev_close": 100 + index,
            "available_at": bar_available_at(day),
        }
        for index, day in enumerate(days)
    ]
    pq.write_table(pa.Table.from_pylist(benchmark), source / "benchmarks.parquet")
    (source / "dataset_metadata.json").write_text(
        json.dumps(
            {
                "source": "Tushare Pro",
                "kind": "real_fixed_basket_baseline_v1",
                "protocol_hash": hash_json(spec.model_dump(mode="json")),
                "test_fixture": True,
            }
        )
    )
    snapshot = DataSnapshotService(store, registry).create_snapshot(str(source), producer="synthetic-test")
    return spec, snapshot


@pytest.mark.parametrize("expression", [None, "rank(delta(close, 10) / delay(close, 10))"])
def test_offline_baseline_records_frozen_lineage_and_reproducible_results(
    tmp_path, fixture_dir, store, registry, session_factory, monkeypatch, expression
):
    def forbid_network(*args, **kwargs):
        raise AssertionError("offline research must not call a vendor")

    monkeypatch.setattr("quant_system.data.ingestion.tushare.TushareClient.query", forbid_network)
    spec, snapshot = synthetic_baseline(tmp_path, fixture_dir, store, registry)
    experiments = ExperimentService(session_factory)
    runner = BaselineRunner(
        store=store,
        registry=registry,
        experiments=experiments,
        rulebook_path=ROOT / "configs" / "markets" / "cn_a.yaml",
        code_commit="test",
        code_hash="test-code-hash",
        environment={"test": "true"},
    )
    report = runner.run(spec, snapshot.artifact_id, strategy_expression=expression)
    payload = json.loads((Path(report.uri.removeprefix("file://")) / "report.json").read_text())
    assert payload["technical_acceptance"] is True
    assert payload["deployment_eligible"] is False
    if expression is not None:
        assert payload["strategy_expression"] == expression
        assert "Candidate" in (Path(report.uri.removeprefix("file://")) / "report.md").read_text()
    assert [segment["name"] for segment in payload["segments"]] == ["development", "validation", "holdout"]
    assert len(registry.find_by_type(ArtifactType.BACKTEST_RESULT)) == 15
    assert snapshot.artifact_id in report.parent_artifact_ids
    assert experiments.get(report.experiment_id).state == ExperimentState.COMPLETED
    assert all(segment["momentum"]["repeat_artifact_id"] for segment in payload["segments"])
    assert all(segment["momentum"]["ledger_checkpoints"] >= 10 for segment in payload["segments"])
    changed = spec.model_copy(update={"lookback_days": 21})
    with pytest.raises(QuantSystemError, match="different research protocol"):
        runner.run(changed, snapshot.artifact_id)


def test_weekly_strategy_skips_non_rebalance_days(provider):
    from quant_system.backtest.feed import MarketDataFeed
    from quant_system.backtest.strategy import StrategyContext
    from quant_system.strategies.weekly_momentum import WeeklyMomentumStrategy

    strategy = WeeklyMomentumStrategy()
    context = StrategyContext(
        trade_date=date(2024, 3, 4),
        as_of=bar_available_at(date(2024, 3, 4)),
        universe=[],
        feed=MarketDataFeed(provider),
        nav=1000000,
        positions={},
        params={},
    )
    strategy._last_week = (2024, 10)
    assert strategy.on_close(context).rebalance is False
