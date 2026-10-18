import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from quant_system.agents.llm import LLMResponse
from quant_system.api.app import create_app
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.domain.enums import ArtifactType
from quant_system.gold.models import PricePoint, Quote
from quant_system.infra.db import make_engine, make_session_factory
from quant_system.infra.settings import Settings


class GoldGateway:
    def __init__(self) -> None:
        self.calls = []

    def complete(self, role, messages):
        self.calls.append(role)
        if role.endswith("_analyst"):
            content = {
                "analyst": role,
                "view": "NEUTRAL",
                "confidence": 0.4,
                "summary": "Only frozen price evidence",
                "claims": [
                    {"claim": "Historical trend only", "evidence_ids": ["gold_return_126"], "confidence": 0.4}
                ],
                "risks": ["No holdout"],
                "open_questions": [],
                "invalidating_conditions": [],
                "missing_evidence": [],
            }
            return LLMResponse(
                content=json.dumps(content),
                provider="test",
                model=f"test-{role}",
                input_tokens=10,
                output_tokens=10,
            )
        content = {
            "researcher": {
                "name": "Gold trend",
                "hypothesis": "Trend persistence",
                "market_view": "Price only",
                "risks": ["No holdout"],
                "parameters": {
                    "fast_window": 20,
                    "slow_window": 60,
                    "volatility_target": 0.12,
                    "trailing_stop": 0.08,
                },
            },
            "critic": {"approved": True, "issues": [], "rationale": "Inspectable"},
            "statistician": {"robust": True, "findings": [], "rationale": "Overoptimistic model"},
            "orchestrator": {
                "recommend_candidate": True,
                "rationale": "Overoptimistic model",
                "next_steps": ["Forward study"],
                "investment_advice": {
                    "horizon": "3至12个月",
                    "stance": "WAIT",
                    "summary": "先等待长期证据补齐。",
                    "reasons": [
                        {"evidence_key": "gold_return_126", "implication": "中期正收益不等于未来收益"},
                        {"evidence_key": "independent_oos", "implication": "仍缺独立样本外"},
                    ],
                    "reassess_when": "长期样本与独立验证补齐时复核。",
                },
            },
            "final_auditor": {
                "decision": "PASS",
                "unsupported_claims": [],
                "missing_risks": [],
                "confidence_adjustment": 0,
                "rationale": "建议与冻结事实一致，未越过证据限制。",
            },
        }[role]
        return LLMResponse(
            content=json.dumps(content),
            provider="test",
            model=f"test-{role}",
            input_tokens=10,
            output_tokens=10,
        )


def test_gold_research_async_contract_and_persisted_report(tmp_path: Path, fixture_dir: str) -> None:
    settings = Settings(
        db_url=f"sqlite:///{tmp_path / 'gold.db'}",
        artifacts_root=str(tmp_path / "artifacts"),
        configs_root="configs",
        fixtures_root=fixture_dir,
    )
    gateway = GoldGateway()
    fetches = []

    def fetcher(as_of, now):
        fetches.append(as_of)
        quotes = [
            Quote(
                symbol=symbol,
                name=symbol,
                kind="spot" if symbol == "Au99.99" else "etf",
                unit="CNY/g" if symbol == "Au99.99" else "CNY/share",
                source="test",
                source_url="https://example.invalid",
                fetched_at=now,
                points=[
                    PricePoint(
                        date=as_of - timedelta(days=199 - index),
                        close=10 + index / 100,
                        open=10 + index / 100,
                    )
                    for index in range(200)
                ],
            )
            for symbol in ("Au99.99", "518880")
        ]
        return quotes, {"fetch": len(fetches)}, []

    client = TestClient(create_app(settings, init_schema=True, agent_gateway=gateway, gold_fetcher=fetcher))
    assert client.get("/api/gold/research/status").json()["ready"] is True
    assert client.get("/api/gold/research/runs").json() == []
    assert client.get("/api/gold/research/runs/missing").status_code == 404
    response = client.post(
        "/api/gold/research/runs", json={"symbol": "518880", "max_weight": 0.10, "focus": "Trend study"}
    )
    assert response.status_code == 202
    assert response.json()["status"] == "QUEUED"
    run_id = response.json()["run_id"]
    run = client.get(f"/api/gold/research/runs/{run_id}").json()
    assert run["status"] == "COMPLETED", run["error"]
    assert run["result"]["outcome"] == "OBSERVE"
    assert run["result"]["local_gates"]["独立样本外验证"] is False
    assert run["result"]["deployment_eligible"] is False
    assert run["result"]["investment_advice"]["horizon"] == "3至12个月"
    assert run["result"]["investment_advice"]["reasons"][1]["value"] is False
    assert run["snapshot_id"] == client.get("/api/gold/dashboard").json()["snapshot"]["artifact_id"]
    assert len(fetches) == 1
    assert set(gateway.calls[:3]) == {"technical_analyst", "quant_analyst", "risk_analyst"}
    assert gateway.calls[3:] == ["orchestrator", "final_auditor"]
    assert run["result"]["deliberation"]["conclusion"] == "INSUFFICIENT_EVIDENCE"
    assert run["result"]["advice_audit"]["status"] == "PASS"
    assert (
        run["result"]["advice_audit"]["published_draft_hash"]
        == run["result"]["advice_audit"]["rounds"][0]["draft_hash"]
    )
    assert client.get("/api/gold/research/status").json()["active_run_id"] is None
    registry = ArtifactRegistry(make_session_factory(make_engine(settings.db_url)))
    assert registry.find_by_type(ArtifactType.GOLD_RESEARCH_RUN) == []
    revisions = registry.find_by_type(ArtifactType.RESEARCH_RUN)
    assert any(artifact.metadata["run_id"] == run_id for artifact in revisions)
    assert {artifact.metadata["task_type"] for artifact in revisions} >= {
        "runtime.plan",
        "gold.collect",
        "gold.research",
    }
    assert registry.find_by_type(ArtifactType.RESEARCH_EVENT)
    receipts = registry.find_by_type(ArtifactType.RESEARCH_CALL)
    assert len({artifact.metadata["call_id"] for artifact in receipts}) == 5
    report = registry.get(run["report_artifact_id"])
    assert (
        report.metadata["approved_publication_hash"] == run["result"]["advice_audit"]["published_draft_hash"]
    )
    reopened = TestClient(create_app(settings, agent_gateway=gateway, gold_fetcher=fetcher))
    assert reopened.get("/api/gold/research/runs").json()[0] == run
    assert reopened.post("/api/gold/research/runs", json={"max_weight": 0.31}).status_code == 422
    assert reopened.post("/api/gold/research/runs", json={"symbol": "000001"}).status_code == 422
    assert reopened.post("/api/gold/research/runs", json={"focus": "   "}).status_code == 422
    spot = reopened.post("/api/gold/research/runs", json={"symbol": "Au99.99"})
    assert spot.status_code == 202
    spot_run = reopened.get(f"/api/gold/research/runs/{spot.json()['run_id']}").json()
    assert spot_run["status"] == "COMPLETED", spot_run["error"]
    assert spot_run["result"]["evaluation"]["unit"] == "CNY/g"
    assert spot_run["result"]["investment_advice"]["stance"] == "WAIT"
