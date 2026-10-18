from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from tests.integration.test_gold_research_api import GoldGateway
from tests.unit.test_macro_ingestion import OfflineTransport
from tests.unit.test_macro_parsers import SAFE_TABLE, SAFE_URL

from quant_system.api.app import create_app
from quant_system.gold.models import PricePoint, Quote
from quant_system.infra.settings import Settings


def _gold_fetcher(as_of, now):
    quote = Quote(
        symbol="Au99.99",
        name="Gold",
        kind="spot",
        unit="CNY/g",
        source="offline",
        source_url="https://example.invalid",
        fetched_at=now,
        points=[
            PricePoint(
                date=as_of - timedelta(days=299 - index), close=700 + index / 10, open=700 + index / 10
            )
            for index in range(300)
        ],
    )
    return [quote], {"source": "offline"}, []


def test_macro_gold_plan_uses_one_runtime_cutoff_budget_and_legacy_projection(
    tmp_path: Path, fixture_dir: str, monkeypatch
) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    gateway = GoldGateway()
    transport = OfflineTransport(
        {
            "https://www.safe.gov.cn/safe/whcb/index.html": f'<a href="{SAFE_URL}">Official reserve assets (2026)</a>'.encode(),
            SAFE_URL: SAFE_TABLE.encode(),
        }
    )
    settings = Settings(
        db_url=f"sqlite:///{tmp_path / 'modular.db'}",
        artifacts_root=str(tmp_path / "artifacts"),
        configs_root="configs",
        fixtures_root=fixture_dir,
    )
    app = create_app(
        settings,
        init_schema=True,
        agent_gateway=gateway,
        gold_fetcher=_gold_fetcher,
        macro_transport=transport,
    )
    client = TestClient(app)
    catalog = client.get("/api/research/modules").json()
    assert {task["module_id"] for task in catalog["modules"]} >= {"gold", "macro", "factor"}
    response = client.post(
        "/api/research/plans",
        json={
            "question": "黄金宏观联合研究",
            "payload": {
                "gold.collect": {"symbol": "Au99.99"},
                "gold.research": {"symbol": "Au99.99"},
                "macro.collect": {"series_ids": ["CN_GOLD_RESERVES"], "start_date": "2026-01-01"},
            },
        },
    )
    assert response.status_code == 200, response.text
    prepared = response.json()
    assert len(prepared["plan"]["tasks"]) == 4
    assert gateway.calls == []
    submitted = client.post(
        "/api/research/runs", json={"plan": prepared["plan"], "idempotency_key": "combined"}
    )
    assert submitted.status_code == 202, submitted.text
    run_id = submitted.json()["run_id"]
    root = client.get(f"/api/research/runs/{run_id}").json()
    assert root["status"] == "COMPLETED", root["error"]
    children = [client.get(f"/api/research/runs/{child_id}").json() for child_id in root["children"]]
    assert {child["knowledge_cutoff"] for child in children} == {root["knowledge_cutoff"]}
    macro = next(child for child in children if child["task_type"] == "macro.analyze")
    gold = next(child for child in children if child["task_type"] == "gold.research")
    assert macro["result"]["outcome"] == "AVAILABLE"
    assert (
        gold["result"]["payload"]["analysis_context"]["macro_snapshot_id"]
        == macro["result"]["payload"]["snapshot_id"]
    )
    assert gold["result"]["payload"]["investment_advice"]["stance"] == "WAIT"
    assert gold["result"]["payload"]["deployment_eligible"] is False
    assert root["usage"]["calls_used"] == len(gateway.calls)
    assert root["usage"]["tokens_used"] == len(gateway.calls) * 20
    receipts = client.get(f"/api/research/runs/{run_id}/calls").json()
    assert len(receipts) == len(gateway.calls)
    assert [receipt["sequence"] for receipt in receipts] == list(range(1, len(receipts) + 1))
    assert client.get(f"/api/gold/research/runs/{run_id}").json()["result"] == gold["result"]["payload"]
    before = len(gateway.calls)
    repeated = client.post(
        "/api/research/runs", json={"plan": prepared["plan"], "idempotency_key": "combined"}
    )
    assert repeated.json()["run_id"] == run_id
    assert len(gateway.calls) == before
    assert client.get("/api/agent-research/runs").json() == []
    assert "offline-secret" not in json.dumps(root)


def test_explicit_gold_entry_expands_collection_and_keeps_legacy_readable(
    tmp_path: Path, fixture_dir: str
) -> None:
    settings = Settings(
        db_url=f"sqlite:///{tmp_path / 'explicit.db'}",
        artifacts_root=str(tmp_path / "artifacts"),
        configs_root="configs",
        fixtures_root=fixture_dir,
    )
    gateway = GoldGateway()
    client = TestClient(
        create_app(settings, init_schema=True, agent_gateway=gateway, gold_fetcher=_gold_fetcher)
    )
    response = client.post(
        "/api/research/runs",
        json={
            "task_type": "gold.research",
            "payload": {"symbol": "Au99.99"},
            "idempotency_key": "explicit-gold",
        },
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    root = client.get(f"/api/research/runs/{run_id}").json()
    assert root["status"] == "COMPLETED", root["error"]
    assert root["request"]["task_type"] == "gold.research"
    assert {node["task_type"] for node in root["execution_plan"]["tasks"]} == {
        "gold.collect",
        "gold.research",
    }
    projected = client.get(f"/api/gold/research/runs/{run_id}").json()
    assert projected["status"] == "COMPLETED"
    assert projected["result"]["investment_advice"]["stance"] == "WAIT"
    assert client.post("/api/research/runs", json=root["request"]).json()["run_id"] == run_id
    assert len(gateway.calls) == 5
