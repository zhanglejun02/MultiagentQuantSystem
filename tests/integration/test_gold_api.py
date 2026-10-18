from datetime import UTC, date, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from quant_system.api.app import create_app
from quant_system.artifacts.envelope import ArtifactEnvelope
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.domain.enums import ArtifactType
from quant_system.gold.models import PricePoint, Quote
from quant_system.gold.service import GoldService, GoldSnapshot
from quant_system.infra.db import make_engine, make_session_factory
from quant_system.infra.settings import Settings


def test_gold_dashboard_and_allocation_persist(tmp_path: Path, fixture_dir: str) -> None:
    settings = Settings(
        db_url=f"sqlite:///{tmp_path / 'gold.db'}",
        artifacts_root=str(tmp_path / "artifacts"),
        configs_root="configs",
        fixtures_root=fixture_dir,
    )
    client = TestClient(create_app(settings, init_schema=True))
    empty = client.get("/api/gold/dashboard")
    assert empty.status_code == 200
    assert empty.json()["snapshot"] is None
    assert empty.json()["summary"]["has_holdings"] is False
    service = GoldService(
        LocalArtifactStore(settings.artifacts_root),
        ArtifactRegistry(make_session_factory(make_engine(settings.db_url))),
    )
    now = datetime.now(UTC)
    spot = Quote(
        symbol="Au99.99",
        name="Gold",
        kind="spot",
        unit="CNY/g",
        source="SGE",
        source_url="https://www.sge.com.cn/",
        fetched_at=now,
        points=[PricePoint(date=date.today(), close=900)],
    )
    etf = Quote(
        symbol="518880",
        name="ETF",
        kind="etf",
        unit="CNY/share",
        source="Test",
        source_url="https://example.com/",
        fetched_at=now,
        points=[PricePoint(date=date.today(), close=10)],
    )
    artifact = service.publish(GoldSnapshot(fetched_at=now, quotes=[spot, etf]), {"test": True})
    record = {
        "other_assets": "90000",
        "target_weight": "0.10",
        "holdings": [{"symbol": "518880", "quantity": "1000", "average_cost": "9"}],
        "watchlist": ["518880"],
        "note": "manual",
    }
    saved = client.post("/api/gold/allocation", json=record)
    assert saved.status_code == 200
    assert saved.json()["summary"]["gold_value"] == "10000"
    assert saved.json()["snapshot"]["artifact_id"] == artifact.artifact_id
    reopened = TestClient(create_app(settings)).get("/api/gold/dashboard").json()
    assert reopened["allocation"]["record"]["note"] == "manual"
    assert reopened["summary"]["weight"] == "0.1"
    assert (
        client.post("/api/gold/allocation", json=record).json()["allocation"]["artifact_id"]
        == reopened["allocation"]["artifact_id"]
    )
    updated = client.post("/api/gold/allocation", json={**record, "note": "updated"}).json()
    assert service.registry.get(updated["allocation"]["artifact_id"]).parent_artifact_ids == (
        reopened["allocation"]["artifact_id"],
    )
    next_snapshot = service.publish(
        GoldSnapshot(fetched_at=now, quotes=[spot, etf]),
        {"test": "next"},
        previous_snapshot_id=artifact.artifact_id,
    )
    assert artifact.artifact_id in next_snapshot.parent_artifact_ids
    assert next_snapshot.data_snapshot_id in next_snapshot.parent_artifact_ids
    service.registry.register(
        ArtifactEnvelope(
            artifact_type=ArtifactType.MARKET_SNAPSHOT,
            producer="test",
            uri="file:///missing-test-gold-market.json",
        )
    )
    degraded = client.get("/api/gold/dashboard")
    assert degraded.status_code == 200
    assert degraded.json()["futures"]["contracts"] == []
    assert any("辅助期货" in warning for warning in degraded.json()["snapshot"]["warnings"])
    assert client.post("/api/gold/allocation", json={**record, "target_weight": "1.1"}).status_code == 422
    assert (
        client.post("/api/gold/allocation", json={**record, "holdings": record["holdings"] * 2}).status_code
        == 422
    )
    Path(next_snapshot.uri.removeprefix("file://")).write_text("{}")
    assert client.get("/api/gold/dashboard").status_code == 500


def test_gold_missing_quote_returns_unknown_value(tmp_path: Path, fixture_dir: str) -> None:
    settings = Settings(
        db_url=f"sqlite:///{tmp_path / 'gold.db'}",
        artifacts_root=str(tmp_path / "artifacts"),
        configs_root="configs",
        fixtures_root=fixture_dir,
    )
    client = TestClient(create_app(settings, init_schema=True))
    response = client.post(
        "/api/gold/allocation",
        json={"holdings": [{"symbol": "518880", "quantity": "1", "average_cost": "9"}]},
    )
    assert response.status_code == 200
    assert response.json()["summary"]["gold_value"] is None
