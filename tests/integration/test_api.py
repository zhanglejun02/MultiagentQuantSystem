from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.helpers import make_engine_with

from quant_system.api.app import create_app
from quant_system.backtest.request import BacktestRequest
from quant_system.infra.settings import Settings
from quant_system.strategies import resolve_strategy


@pytest.fixture()
def client(tmp_path: Path, provider, rulebook):
    settings = Settings(
        db_url=f"sqlite:///{tmp_path / 'api.db'}",
        artifacts_root=str(tmp_path / "artifacts"),
        configs_root="configs",
        fixtures_root=provider.data_dir.as_posix(),
    )
    app = create_app(settings, init_schema=True)
    # seed one backtest into the same DB the API reads
    from quant_system.artifacts.registry import ArtifactRegistry
    from quant_system.artifacts.store import LocalArtifactStore
    from quant_system.infra.db import make_engine, make_session_factory

    sf = make_session_factory(make_engine(settings.db_url))
    registry = ArtifactRegistry(sf)
    store = LocalArtifactStore(settings.artifacts_root)
    engine = make_engine_with(provider, rulebook, store, registry,
                              resolve_strategy("momentum_topk", {}))
    result = engine.run(BacktestRequest(
        strategy_id="momentum_topk", dataset_snapshot_id="",
        start_date=date(2024, 1, 1), end_date=date(2024, 2, 15),
        initial_cash=Decimal("1000000"),
    ))
    return TestClient(app), result


def test_health(client):
    c, _ = client
    resp = c.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["db"] == "ok"


def test_backtest_endpoints(client):
    c, result = client
    listed = c.get("/api/backtests").json()
    assert any(b["artifact_id"] == result.result_artifact_id for b in listed)

    metrics = c.get(f"/api/backtests/{result.result_artifact_id}/metrics").json()
    assert "performance" in metrics and "execution" in metrics

    equity = c.get(f"/api/backtests/{result.result_artifact_id}/equity").json()
    assert equity and {"date", "nav"} <= set(equity[0])

    orders = c.get(f"/api/backtests/{result.result_artifact_id}/orders").json()
    assert orders and "client_order_id" in orders[0]


def test_lineage_endpoint(client):
    c, result = client
    graph = c.get(f"/api/artifacts/{result.result_artifact_id}/lineage").json()
    assert any(a["artifact_id"] == result.spec_artifact_id for a in graph["ancestors"])


def test_experiment_flow(client):
    c, _ = client
    exp = c.post("/api/experiments", json={"name": "api test"}).json()
    assert exp["state"] == "IDEA"
    ok = c.post(f"/api/experiments/{exp['experiment_id']}/transition",
                json={"target": "PROPOSED"})
    assert ok.status_code == 200
    bad = c.post(f"/api/experiments/{exp['experiment_id']}/transition",
                 json={"target": "LIVE"})
    assert bad.status_code == 409


def test_factor_registry_flow(client):
    c, _ = client
    created = c.post("/api/factors", json={
        "factor_key": "momentum_20d",
        "version": "1.0.0",
        "name": "20日动量",
        "category": "momentum",
        "owner": "research",
        "code_ref": "quant_system.strategies.momentum_topk",
        "metrics": {"rank_ic": 0.041, "coverage": 0.98},
    })
    assert created.status_code == 201
    factor = created.json()
    assert factor["state"] == "DRAFT"
    assert c.get("/api/factors").json()[0]["factor_key"] == "momentum_20d"

    ok = c.post(f"/api/factors/{factor['factor_id']}/transition", json={
        "target": "VALIDATED", "reason": "PIT and stability checks passed",
    })
    assert ok.status_code == 200
    bad = c.post(f"/api/factors/{factor['factor_id']}/transition", json={"target": "ACTIVE"})
    assert bad.status_code == 409


def test_factor_evaluation_flow(client):
    c, _ = client
    response = c.post("/api/factor-runs/evaluate", json={
        "expression": "rank(-delta(close, 5) / ts_std(return_1d, 20))",
        "universe_id": "cn_a_all",
        "start_date": "2024-02-01",
        "end_date": "2024-05-31",
        "target": "NEXT_OPEN_TO_CLOSE",
        "horizon": 1,
    })
    assert response.status_code == 201
    run = response.json()
    assert run["status"] == "COMPLETED"
    assert run["metrics"]["n_ic_days"] > 0
    assert run["ic_series"]
    assert c.get("/api/factor-runs").json()[0]["run_id"] == run["run_id"]


def test_system_status_and_killswitch(client):
    c, _ = client
    status = c.get("/api/system/status").json()
    assert status["runtime_state"] == "RUNNING"
    c.post("/api/system/killswitch", params={"engage": True, "reason": "drill"})
    assert c.get("/api/system/status").json()["kill_switch"] is True
    c.post("/api/system/killswitch", params={"engage": False, "operator": "tester"})
    assert c.get("/api/system/status").json()["kill_switch"] is False
