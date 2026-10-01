from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from quant_system.api.app import create_app
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.data.pipeline import DailyMarketPipeline
from quant_system.infra.db import make_engine, make_session_factory
from quant_system.infra.settings import Settings


def _settings(tmp_path: Path, fixture_dir: str) -> Settings:
    return Settings(
        db_url=f"sqlite:///{tmp_path / 'market-api.db'}",
        artifacts_root=str(tmp_path / "artifacts"),
        configs_root="configs",
        fixtures_root=fixture_dir,
    )


def _datasets(trade_date: date) -> dict[str, list[dict]]:
    day = trade_date.isoformat()
    return {
        "equity_daily": [
            {
                "symbol": "600000.SSE",
                "trade_date": day,
                "open": 10,
                "high": 10.5,
                "low": 9.9,
                "close": 10.2,
                "prev_close": 10,
                "volume": 100,
                "amount": 1_000,
            }
        ],
        "futures_daily": [
            {
                "symbol": "rb2701.SHFE",
                "product": "rb",
                "trading_day": day,
                "open": 3500,
                "high": 3550,
                "low": 3480,
                "close": 3520,
                "settlement": 3518,
                "pre_settlement": 3490,
                "volume": 100_000,
                "open_interest": 250_000,
            }
        ],
    }


def test_market_snapshot_read_api(tmp_path: Path, fixture_dir: str) -> None:
    settings = _settings(tmp_path, fixture_dir)
    app = create_app(settings, init_schema=True)
    registry = ArtifactRegistry(make_session_factory(make_engine(settings.db_url)))
    pipeline = DailyMarketPipeline(LocalArtifactStore(settings.artifacts_root), registry)
    data_dir = tmp_path / "normalized"
    data_dir.mkdir()
    datasets = _datasets(date.today())
    (data_dir / "dataset.json").write_text(json.dumps(datasets))
    published = pipeline.publish(
        trade_date=date.today(),
        data_dir=data_dir,
        datasets=datasets,
        required_datasets=("equity_daily", "futures_daily"),
        expected_equity_symbols=1,
    )
    assert published.market_snapshot is not None

    client = TestClient(app)
    status = client.get("/api/market-data/status").json()
    assert status["ready"] is True
    assert status["stale"] is False
    assert status["quality_status"] == "VERIFIED"
    assert status["market_snapshot_id"] == published.market_snapshot.artifact_id

    history = client.get("/api/market-snapshots").json()
    assert history[0]["artifact_id"] == published.market_snapshot.artifact_id
    latest = client.get("/api/market-snapshots/latest")
    assert latest.status_code == 200
    assert latest.json()["snapshot"]["trade_date"] == date.today().isoformat()
    detail = client.get(f"/api/market-snapshots/{published.market_snapshot.artifact_id}")
    assert detail.json() == latest.json()


def test_market_snapshot_api_reports_empty_state(tmp_path: Path, fixture_dir: str) -> None:
    client = TestClient(create_app(_settings(tmp_path, fixture_dir), init_schema=True))

    status = client.get("/api/market-data/status").json()
    assert status == {
        "ready": False,
        "latest_trade_date": None,
        "stale": True,
        "quality_status": None,
        "quality_report_id": None,
        "market_snapshot_id": None,
        "data_snapshot_id": None,
    }
    assert client.get("/api/market-snapshots/latest").status_code == 404


def test_market_snapshot_api_reports_missing_content(
    tmp_path: Path, fixture_dir: str
) -> None:
    settings = _settings(tmp_path, fixture_dir)
    app = create_app(settings, init_schema=True)
    registry = ArtifactRegistry(make_session_factory(make_engine(settings.db_url)))
    pipeline = DailyMarketPipeline(LocalArtifactStore(settings.artifacts_root), registry)
    data_dir = tmp_path / "normalized"
    data_dir.mkdir()
    datasets = _datasets(date.today())
    (data_dir / "dataset.json").write_text(json.dumps(datasets))
    published = pipeline.publish(
        trade_date=date.today(),
        data_dir=data_dir,
        datasets=datasets,
        required_datasets=("equity_daily", "futures_daily"),
        expected_equity_symbols=1,
    )
    assert published.market_snapshot is not None
    Path(published.market_snapshot.uri.removeprefix("file://")).unlink()

    client = TestClient(app, raise_server_exceptions=False)
    latest = client.get("/api/market-snapshots/latest")
    status = client.get("/api/market-data/status")

    assert latest.status_code == 500
    assert "content is missing" in latest.json()["detail"]
    assert status.status_code == 500
    assert "content is missing" in status.json()["detail"]


def test_market_snapshot_api_reports_malformed_content(
    tmp_path: Path, fixture_dir: str
) -> None:
    settings = _settings(tmp_path, fixture_dir)
    app = create_app(settings, init_schema=True)
    registry = ArtifactRegistry(make_session_factory(make_engine(settings.db_url)))
    pipeline = DailyMarketPipeline(LocalArtifactStore(settings.artifacts_root), registry)
    data_dir = tmp_path / "normalized"
    data_dir.mkdir()
    datasets = _datasets(date.today())
    (data_dir / "dataset.json").write_text(json.dumps(datasets))
    published = pipeline.publish(
        trade_date=date.today(),
        data_dir=data_dir,
        datasets=datasets,
        required_datasets=("equity_daily", "futures_daily"),
        expected_equity_symbols=1,
    )
    assert published.market_snapshot is not None
    Path(published.market_snapshot.uri.removeprefix("file://")).write_text("not-json")

    response = TestClient(app, raise_server_exceptions=False).get(
        f"/api/market-snapshots/{published.market_snapshot.artifact_id}"
    )

    assert response.status_code == 500
    assert "content is malformed" in response.json()["detail"]