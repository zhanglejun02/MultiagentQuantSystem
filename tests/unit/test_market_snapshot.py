from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from quant_system.artifacts.store import LocalArtifactStore
from quant_system.data.pipeline import DailyMarketPipeline
from quant_system.domain.enums import ArtifactStatus, ArtifactType


def _datasets() -> dict[str, list[dict]]:
    return {
        "equity_daily": [
            {
                "symbol": "600000.SSE",
                "trade_date": "2026-09-21",
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "close": 10.2,
                "prev_close": 10.0,
                "volume": 100,
                "amount": 1_000,
            },
            {
                "symbol": "000001.SZSE",
                "trade_date": "2026-09-21",
                "open": 12.0,
                "high": 12.1,
                "low": 11.5,
                "close": 11.8,
                "prev_close": 12.0,
                "volume": 200,
                "amount": 2_000,
            },
        ],
        "futures_daily": [
            {
                "symbol": "rb2701.SHFE",
                "product": "rb",
                "trading_day": "2026-09-21",
                "open": 3500,
                "high": 3550,
                "low": 3480,
                "close": 3520,
                "settlement": 3518,
                "pre_settlement": 3490,
                "volume": 100_000,
                "open_interest": 250_000,
            },
            {
                "symbol": "rb2705.SHFE",
                "product": "rb",
                "trading_day": "2026-09-21",
                "open": 3600,
                "high": 3620,
                "low": 3580,
                "close": 3610,
                "settlement": 3605,
                "pre_settlement": 3590,
                "volume": 80_000,
                "open_interest": 200_000,
            },
        ],
    }


def test_pipeline_publishes_quality_data_and_market_snapshot(tmp_path: Path, registry) -> None:
    data_dir = tmp_path / "normalized"
    data_dir.mkdir()
    (data_dir / "dataset.json").write_text(json.dumps(_datasets(), sort_keys=True))
    pipeline = DailyMarketPipeline(LocalArtifactStore(str(tmp_path / "artifacts")), registry)

    result = pipeline.publish(
        trade_date=date(2026, 9, 21),
        data_dir=data_dir,
        datasets=_datasets(),
        required_datasets=("equity_daily", "futures_daily"),
        expected_equity_symbols=2,
    )

    assert result.passed
    assert result.quality_report.status == ArtifactStatus.VERIFIED
    assert result.data_snapshot is not None
    assert result.market_snapshot is not None
    assert result.data_snapshot.parent_artifact_ids == (result.quality_report.artifact_id,)
    assert result.market_snapshot.parent_artifact_ids == (result.data_snapshot.artifact_id,)
    assert result.market_snapshot.data_snapshot_id == result.data_snapshot.artifact_id

    payload = pipeline.read_market_snapshot(result.market_snapshot)
    assert payload["equity"]["advancers"] == 1
    assert payload["equity"]["decliners"] == 1
    assert payload["equity"]["total_amount"] == 3_000.0
    assert payload["futures"]["dominant_contracts"][0]["symbol"] == "rb2701.SHFE"

    rerun = pipeline.publish(
        trade_date=date(2026, 9, 21),
        data_dir=data_dir,
        datasets=_datasets(),
        required_datasets=("equity_daily", "futures_daily"),
        expected_equity_symbols=2,
    )
    assert rerun.market_snapshot is not None
    assert rerun.quality_report.artifact_id == result.quality_report.artifact_id
    assert rerun.data_snapshot is not None
    assert rerun.data_snapshot.artifact_id == result.data_snapshot.artifact_id
    assert rerun.market_snapshot.artifact_id == result.market_snapshot.artifact_id
    assert rerun.market_snapshot.content_hash == result.market_snapshot.content_hash
    assert rerun.market_snapshot.uri == result.market_snapshot.uri


def test_pipeline_stops_after_failed_quality_report(tmp_path: Path, registry) -> None:
    data_dir = tmp_path / "normalized"
    data_dir.mkdir()
    (data_dir / "dataset.json").write_text("{}")
    pipeline = DailyMarketPipeline(LocalArtifactStore(str(tmp_path / "artifacts")), registry)

    result = pipeline.publish(
        trade_date=date(2026, 9, 21),
        data_dir=data_dir,
        datasets={},
        required_datasets=("equity_daily", "futures_daily"),
    )

    assert not result.passed
    assert result.quality_report.status == ArtifactStatus.FAILED
    assert result.data_snapshot is None
    assert result.market_snapshot is None
    assert registry.find_by_type(ArtifactType.DATA_SNAPSHOT) == []
    assert registry.find_by_type(ArtifactType.MARKET_SNAPSHOT) == []


def test_pipeline_asset_modes_keep_futures_as_an_explicit_hard_gate(
    tmp_path: Path, registry
) -> None:
    data_dir = tmp_path / "normalized"
    data_dir.mkdir()
    datasets = {"equity_daily": _datasets()["equity_daily"], "futures_daily": []}
    (data_dir / "dataset.json").write_text(json.dumps(datasets, sort_keys=True))
    pipeline = DailyMarketPipeline(LocalArtifactStore(str(tmp_path / "artifacts")), registry)

    equity_only = pipeline.publish(
        trade_date=date(2026, 9, 21),
        data_dir=data_dir,
        datasets=datasets,
        required_datasets=("equity_daily",),
        optional_datasets=("futures_daily",),
        expected_equity_symbols=2,
    )
    combined = pipeline.publish(
        trade_date=date(2026, 9, 21),
        data_dir=data_dir,
        datasets=datasets,
        required_datasets=("equity_daily", "futures_daily"),
        expected_equity_symbols=2,
    )

    assert equity_only.passed
    assert equity_only.market_snapshot is not None
    assert not combined.passed
    assert combined.market_snapshot is None