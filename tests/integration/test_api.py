from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.helpers import make_engine_with

from quant_system.agents.llm import LLMResponse
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
    app = create_app(settings, init_schema=True, agent_gateway=FakeAgentGateway())
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


class FakeAgentGateway:
    responses = {
        "researcher": {
            "name": "20日动量",
            "hypothesis": "中期趋势延续",
            "expression": "rank(delta(close, 20))",
            "horizon": 5,
        },
        "critic": {"approved": True, "issues": [], "rationale": "PIT-safe"},
        "statistician": {"robust": True, "findings": ["stable"], "rationale": "usable"},
        "orchestrator": {
            "recommend_candidate": True,
            "rationale": "reviews complete",
            "next_steps": ["paper validation"],
        },
    }

    def complete(self, role, messages):
        return LLMResponse(
            content=json.dumps(self.responses[role], ensure_ascii=False),
            provider="fake",
            model=f"fake-{role}",
            input_tokens=10,
            output_tokens=5,
        )


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


def test_agent_research_flow(client):
    c, _ = client
    status = c.get("/api/agent-research/status")
    assert status.status_code == 200
    assert set(status.json()["roles"]) == {
        "researcher", "critic", "statistician", "orchestrator"
    }

    response = c.post("/api/agent-research/runs", json={
        "topic": "寻找一个可解释的中期量价因子",
        "start_date": "2024-02-01",
        "end_date": "2024-05-31",
    })
    assert response.status_code == 201
    result = response.json()
    assert result["experiment"]["state"] == "CANDIDATE"
    assert [call["role"] for call in result["calls"]] == [
        "researcher", "critic", "statistician", "orchestrator"
    ]
    reports = c.get("/api/agent-research/runs").json()
    assert reports[0]["artifact_id"] == result["report_artifact_id"]


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
    assert factor["state"] == "PROPOSED"
    assert c.get("/api/factors").json()[0]["factor_key"] == "momentum_20d"

    blocked = c.post(f"/api/factors/{factor['factor_id']}/transition", json={
        "target": "SCREENED", "reason": "manual promotion attempt",
    })
    assert blocked.status_code == 409
    retired = c.post(f"/api/factors/{factor['factor_id']}/transition", json={
        "target": "DEPRECATED", "reason": "operator retired factor",
    })
    assert retired.status_code == 200


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


def test_futures_paper_simulation_flow(client):
    c, _ = client
    contracts = c.get("/api/futures/contracts")
    assert contracts.status_code == 200
    assert {item["symbol"] for item in contracts.json()} == {
        "rb2701.SHFE", "m2701.DCE", "MA701.CZCE"
    }

    created = c.post("/api/futures/simulations", json={
        "name": "API futures test",
        "initial_balance": "100000",
        "mode": "PAPER",
    })
    assert created.status_code == 201
    simulation_id = created.json()["simulation_id"]
    order = c.post(f"/api/futures/simulations/{simulation_id}/orders", json={
        "client_order_id": "api-open-rb",
        "symbol": "rb2701.SHFE",
        "direction": "LONG",
        "offset": "OPEN",
        "quantity": 2,
        "order_type": "MARKET",
    })
    assert order.status_code == 201
    assert order.json()["status"] == "ACCEPTED"

    advanced = c.post(f"/api/futures/simulations/{simulation_id}/advance")
    assert advanced.status_code == 200
    assert advanced.json()["trade_count"] == 1
    assert advanced.json()["positions"][0]["long_yesterday"] == 2
    assert len(c.get(f"/api/futures/simulations/{simulation_id}/trades").json()) == 1
    assert len(c.get(f"/api/futures/simulations/{simulation_id}/events").json()) == 3


def test_futures_api_rejects_live_mode_and_respects_killswitch(client):
    c, _ = client
    live = c.post("/api/futures/simulations", json={
        "name": "must fail", "initial_balance": "100000", "mode": "LIVE"
    })
    assert live.status_code == 409

    simulation = c.post("/api/futures/simulations", json={
        "name": "kill test", "initial_balance": "100000"
    }).json()
    c.post("/api/system/killswitch", params={"engage": True, "reason": "test"})
    order = c.post(f"/api/futures/simulations/{simulation['simulation_id']}/orders", json={
        "symbol": "m2701.DCE",
        "direction": "SHORT",
        "offset": "OPEN",
        "quantity": 1,
    })
    assert order.status_code == 201
    assert order.json()["status"] == "REJECTED"
    assert "KillSwitch" in order.json()["reject_reason"]


def test_futures_backtest_api_uses_distinct_artifacts(client):
    c, _ = client
    response = c.post("/api/futures/backtests", json={
        "symbol": "rb2701.SHFE",
        "initial_balance": "100000",
        "lots": 2,
        "fast_window": 2,
        "slow_window": 3,
    })
    assert response.status_code == 201
    result = response.json()
    assert result["metrics"]["trade_count"] > 0

    listed = c.get("/api/futures/backtests").json()
    assert listed[0]["artifact_id"] == result["result_artifact_id"]
    assert listed[0]["artifact_type"] == "FUTURES_BACKTEST_RESULT"
    metrics = c.get(
        f"/api/futures/backtests/{result['result_artifact_id']}/metrics"
    )
    assert metrics.status_code == 200
    assert metrics.json()["final_equity"] == result["metrics"]["final_equity"]
