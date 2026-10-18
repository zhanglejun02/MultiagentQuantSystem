import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest
from pydantic import BaseModel, ValidationError

from quant_system.agents.config import AgentLLMConfig
from quant_system.agents.llm import LLMResponse
from quant_system.artifacts.envelope import ArtifactEnvelope, hash_bytes, hash_json
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.domain.enums import ArtifactType
from quant_system.domain.exceptions import ArtifactNotFoundError, QuantSystemError
from quant_system.gold.models import PricePoint, Quote
from quant_system.gold.research import (
    ROLES,
    GoldResearchBusy,
    GoldResearchRequest,
    GoldResearchRun,
    GoldResearchService,
    GoldTaskRequest,
)
from quant_system.gold.service import GoldService
from quant_system.infra.db import create_all, make_engine, make_session_factory
from quant_system.research.runtime.calls import BudgetLimits
from quant_system.research.runtime.engine import ResearchConflict, ResearchRuntime
from quant_system.research.runtime.models import (
    EvidenceRef,
    InputRef,
    PlanTask,
    ResearchPlan,
    RunRequest,
    TaskDescriptor,
    TaskResult,
)
from quant_system.research.runtime.ports import ModuleRegistry, ResearchTask
from quant_system.research.runtime.storage import ArtifactRunRepository


class EmptyRequest(BaseModel):
    pass


class FakeGoldGateway:
    def __init__(self) -> None:
        self.roles = []
        self.responses = {
            "researcher": {
                "name": "Gold trend",
                "hypothesis": "Trend may persist",
                "market_view": "Price evidence only",
                "risks": ["No independent holdout"],
                "parameters": {
                    "fast_window": 20,
                    "slow_window": 60,
                    "volatility_target": 0.12,
                    "trailing_stop": 0.08,
                },
            },
            "critic": {"approved": True, "issues": [], "rationale": "Rules are inspectable"},
            "statistician": {"robust": False, "findings": ["No holdout"], "rationale": "Diagnostic only"},
            "orchestrator": {
                "recommend_candidate": True,
                "rationale": "Model opinion cannot override gates",
                "next_steps": ["Forward paper study"],
                "investment_advice": {
                    "horizon": "3至12个月",
                    "stance": "WAIT",
                    "summary": "等待长期证据补齐，暂不扩大黄金配置。",
                    "reasons": [
                        {"evidence_key": "gold_return_126", "implication": "中期趋势为正但不能外推未来收益"},
                        {"evidence_key": "independent_oos", "implication": "策略缺少独立验证"},
                    ],
                    "reassess_when": "补足一年以上观测并重新检验长期趋势。",
                },
            },
            "final_auditor": {
                "decision": "PASS",
                "unsupported_claims": [],
                "missing_risks": [],
                "confidence_adjustment": 0,
                "rationale": "建议没有超出冻结事实，保留证据不足限制。",
            },
        }

    def complete(self, role, messages):
        self.roles.append(role)
        if role.endswith("_analyst"):
            state = {
                "analyst": role,
                "view": "NEUTRAL",
                "confidence": 0.4,
                "summary": "Price evidence only",
                "claims": [
                    {
                        "claim": "Historical trend does not predict future returns",
                        "evidence_ids": ["gold_return_126"],
                        "confidence": 0.4,
                    }
                ],
                "risks": ["No independent holdout"],
                "open_questions": [],
                "invalidating_conditions": [],
                "missing_evidence": [],
            }
            return LLMResponse(
                content=json.dumps(state),
                provider="fake",
                model=f"fake-{role}",
                input_tokens=100,
                output_tokens=50,
                raw_request_id=f"request-{len(self.roles)}",
            )
        return LLMResponse(
            content=json.dumps(self.responses[role]),
            provider="fake",
            model=f"fake-{role}",
            input_tokens=100,
            output_tokens=50,
            raw_request_id=f"request-{len(self.roles)}",
        )


def _config(**budget) -> AgentLLMConfig:
    return AgentLLMConfig.model_validate(
        {
            "providers": {
                "fake": {
                    "kind": "openai_compatible",
                    "api_key_env": "GOLD_FAKE_KEY",
                    "base_url": "https://example.invalid",
                }
            },
            "roles": {
                role: {"provider": "fake", "model": f"fake-{role}", "max_output_tokens": 1024}
                for role in (
                    "technical_analyst",
                    "macro_analyst",
                    "quant_analyst",
                    "risk_analyst",
                    "orchestrator",
                    "final_auditor",
                )
            },
            "research_budget": budget,
        }
    )


def _quotes(as_of: date, now: datetime, observations: int = 200) -> list[Quote]:
    return [
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
                    date=as_of - timedelta(days=observations - 1 - index),
                    close=10 + index / 100,
                    open=10 + index / 100,
                    high=10 + index / 100,
                    low=10 + index / 100,
                )
                for index in range(observations)
            ],
        )
        for symbol in ("Au99.99", "518880")
    ]


def _service(
    tmp_path: Path,
    *,
    config=None,
    gateway=None,
    fetcher=None,
    clock=None,
    macro_evidence_reader=None,
) -> GoldResearchService:
    engine = make_engine(f"sqlite:///{tmp_path / 'gold.db'}")
    create_all(engine)
    gold = GoldService(
        LocalArtifactStore(str(tmp_path / "artifacts")), ArtifactRegistry(make_session_factory(engine))
    )
    return GoldResearchService(
        gold,
        config or _config(),
        gateway or FakeGoldGateway(),
        fetcher=fetcher or (lambda as_of, now: (_quotes(as_of, now), {"fetched_at": now.isoformat()}, [])),
        clock=clock or (lambda: datetime.now(UTC)),
        credentials_required=False,
        macro_evidence_reader=macro_evidence_reader,
    )


def test_independent_roles_use_one_new_snapshot_and_persist_auditable_history(tmp_path: Path) -> None:
    fetches = []

    def fetcher(as_of, now):
        fetches.append(as_of)
        return _quotes(as_of, now), {"fetch": len(fetches)}, []

    gateway = FakeGoldGateway()
    service = _service(tmp_path, fetcher=fetcher, gateway=gateway)
    queued = service.create(GoldResearchRequest(symbol="518880"))
    assert service.status()["active_run_id"] == queued.run_id
    service.execute(queued.run_id)
    run = service.get(queued.run_id)
    assert run.status == "COMPLETED", run.error
    assert set(gateway.roles[:3]) == {"technical_analyst", "quant_analyst", "risk_analyst"}
    assert gateway.roles[3:] == ["orchestrator", "final_auditor"]
    assert run.result["outcome"] == "OBSERVE"
    assert run.result["deployment_eligible"] is False
    assert run.result["evaluation"]["signal"] == "ENTER"
    assert run.result["local_gates"]["统计员未否决"] is False
    advice = run.result["investment_advice"]
    assert advice["horizon"] == "3至12个月"
    assert advice["stance"] == "WAIT"
    assert advice["confidence"] == "LOW"
    assert (
        advice["reasons"][0]["value"]
        == run.result["analysis_context"]["facts"]["investment_advice_evidence"]["gold_return_126"]
    )
    assert advice["reasons"][1]["value"] is False
    assert {call["raw_request_id"] for call in run.result["calls"]} == {
        f"request-{index}" for index in range(1, 6)
    }
    report = service.gold.registry.get(run.report_artifact_id)
    assert run.snapshot_id in report.parent_artifact_ids
    assert report.data_snapshot_id == service.gold.registry.get(run.snapshot_id).data_snapshot_id
    assert service.gold.registry.get(report.data_snapshot_id).artifact_type == ArtifactType.DATA_SNAPSHOT
    payload = service.gold.store.get_json(report.uri)
    assert len(payload["audit"]) == 5
    assert payload["audit"][0]["messages"][1]["content"]
    assert payload["audit"][0]["response"]["content"]
    assert payload["budget"]["dollar_cost_verified"] is False
    stages = {event.stage for event in service.runtime.events(run.run_id)}
    assert {
        "queued",
        "fetch",
        "evaluation",
        "independent_analysis",
        "cross_review",
        "orchestrator",
        "completed",
    } <= stages
    assert run.result["deliberation"]["conclusion"] == "INSUFFICIENT_EVIDENCE"
    assert run.result["deliberation"]["skipped_analysts"]["macro_analyst"]
    assert payload["protocol"] == "gold-deliberation-v4"
    assert service.gold.registry.find_by_type(ArtifactType.GOLD_RESEARCH_RUN) == []
    revisions = service.gold.registry.find_by_type(ArtifactType.RESEARCH_RUN)
    assert revisions
    assert len(service.runtime.calls(run.run_id)) == 5
    reopened = _service(tmp_path)
    assert reopened.get(run.run_id) == run
    assert reopened.status()["active_run_id"] is None
    second = reopened.create(GoldResearchRequest())
    reopened.fetcher = fetcher
    reopened.execute(second.run_id)
    assert len(fetches) == 2
    assert reopened.get(second.run_id).snapshot_id != run.snapshot_id
    assert len(reopened.list_runs()) == 2


def test_run_lock_and_interrupted_job_recovery_do_not_duplicate_calls(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run = service.create(GoldResearchRequest())
    with pytest.raises(GoldResearchBusy):
        service.create(GoldResearchRequest())
    reopened = _service(tmp_path)
    interrupted = reopened.get(run.run_id)
    assert interrupted.status == "FAILED"
    assert "不会自动重试" in interrupted.error
    assert reopened.status()["active_run_id"] is None
    assert reopened.create(GoldResearchRequest()).status == "QUEUED"


@pytest.mark.parametrize(
    "budget, expected_calls", [({"max_llm_calls_per_run": 3}, 3), ({"max_total_tokens_per_run": 1}, 0)]
)
def test_budget_is_checked_before_the_next_paid_call(tmp_path: Path, budget, expected_calls) -> None:
    gateway = FakeGoldGateway()
    service = _service(tmp_path, config=_config(**budget), gateway=gateway)
    run = service.create(GoldResearchRequest())
    service.execute(run.run_id)
    failed = service.get(run.run_id)
    assert failed.status == "FAILED"
    assert "budget" in failed.error
    assert len(gateway.roles) == expected_calls
    assert failed.snapshot_id is not None
    assert service.status()["active_run_id"] is None
    assert service.create(GoldResearchRequest()).status == "QUEUED"


def test_refetched_but_stale_or_missing_etf_never_calls_models(tmp_path: Path) -> None:
    gateway = FakeGoldGateway()

    def stale(as_of, now):
        return _quotes(as_of - timedelta(days=10), now), {}, []

    service = _service(tmp_path, gateway=gateway, fetcher=stale)
    run = service.create(GoldResearchRequest(symbol="518880"))
    service.execute(run.run_id)
    assert service.get(run.run_id).status == "FAILED"
    assert "4个自然日" in service.get(run.run_id).error
    assert gateway.roles == []
    service.fetcher = lambda as_of, now: (_quotes(as_of, now)[:1], {}, ["ETF source failed"])
    run = service.create(GoldResearchRequest(symbol="518880"))
    service.execute(run.run_id)
    assert service.get(run.run_id).status == "FAILED"
    assert "旧报价" in service.get(run.run_id).error
    assert gateway.roles == []


def test_mismatched_instrument_dates_stop_before_any_paid_call(tmp_path: Path) -> None:
    gateway = FakeGoldGateway()

    def fetcher(as_of, now):
        spot, selected = _quotes(as_of, now)
        return [spot, selected.model_copy(update={"points": selected.points[:-1]})], {}, []

    service = _service(tmp_path, gateway=gateway, fetcher=fetcher)
    queued = service.create(GoldResearchRequest(symbol="518880"))
    service.execute(queued.run_id)
    run = service.get(queued.run_id)
    assert run.status == "FAILED"
    assert "日期不一致" in run.error
    assert gateway.roles == []


def test_credentials_are_required_for_real_gateway_use(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GOLD_FAKE_KEY", raising=False)
    service = _service(tmp_path)
    service.credentials_required = True
    assert service.status()["ready"] is False
    with pytest.raises(QuantSystemError, match="凭据"):
        service.create(GoldResearchRequest())


def test_spot_research_does_not_require_an_etf_and_uses_gram_units(tmp_path: Path) -> None:
    service = _service(
        tmp_path, fetcher=lambda as_of, now: (_quotes(as_of, now)[:1], {}, ["ETF unavailable"])
    )
    queued = service.create(GoldResearchRequest())
    assert queued.request.symbol == "Au99.99"
    service.execute(queued.run_id)
    run = service.get(queued.run_id)
    assert run.status == "COMPLETED", run.error
    assert run.result["analysis_context"]["facts"]["selected_instrument"]["kind"] == "spot"
    evaluation = run.result["evaluation"]
    assert evaluation["symbol"] == "Au99.99"
    assert evaluation["unit"] == "CNY/g"
    assert evaluation["instrument_kind"] == "spot"
    assert "金价参考组合" in evaluation["rules"]["execution"]
    assert any("回购费用" in item for item in evaluation["limitations"])
    assert run.result["deployment_eligible"] is False


@pytest.mark.parametrize(
    "invalid_reason", ["unsupported_stance", "missing_evidence", "no_longterm_fact", "misstated_drawdown"]
)
def test_longterm_advice_cannot_ignore_local_evidence(tmp_path: Path, invalid_reason: str) -> None:
    gateway = FakeGoldGateway()
    advice = gateway.responses["orchestrator"]["investment_advice"]
    if invalid_reason == "unsupported_stance":
        advice["stance"] = "GRADUAL_ALLOCATION"
    elif invalid_reason == "missing_evidence":
        advice["reasons"][0]["evidence_key"] = "gold_return_252"
    elif invalid_reason == "no_longterm_fact":
        advice["reasons"][0]["evidence_key"] = "double_cost_return"
    else:
        advice["reasons"][0] = {
            "evidence_key": "gold_drawdown_252",
            "implication": "252日最大回撤说明风险偏高",
        }
    service = _service(
        tmp_path,
        gateway=gateway,
        fetcher=lambda as_of, now: (
            _quotes(as_of, now, 260 if invalid_reason == "misstated_drawdown" else 200),
            {},
            [],
        ),
    )
    run = service.create(GoldResearchRequest())
    service.execute(run.run_id)
    failed = service.get(run.run_id)
    assert failed.status == "FAILED"
    attempts = [receipt for receipt in service.runtime.calls(run.run_id) if receipt.role == "orchestrator"]
    assert len(attempts) == 2
    assert all(receipt.status == "INVALID" and receipt.parse_error for receipt in attempts)
    assert failed.error == attempts[-1].parse_error
    assert gateway.roles.count("orchestrator") == 2
    assert service.status()["active_run_id"] is None


@pytest.mark.parametrize("rising", [True, False])
def test_price_trend_cannot_override_insufficient_evidence(tmp_path: Path, rising: bool) -> None:
    gateway = FakeGoldGateway()
    advice = gateway.responses["orchestrator"]["investment_advice"]
    advice["stance"] = "GRADUAL_ALLOCATION"
    advice["reasons"] = [
        {"evidence_key": "gold_return_126", "implication": "中期价格趋势支持条件式分批判断"},
        {"evidence_key": "gold_ma250_gap", "implication": "长期价格位置支持趋势判断"},
    ]

    def fetcher(as_of, now):
        quotes = _quotes(as_of, now, 300)
        if not rising:
            quotes = [
                quote.model_copy(
                    update={
                        "points": [
                            point.model_copy(update={"date": original.date})
                            for point, original in zip(reversed(quote.points), quote.points, strict=True)
                        ]
                    }
                )
                for quote in quotes
            ]
        return quotes, {}, []

    service = _service(tmp_path, gateway=gateway, fetcher=fetcher)
    queued = service.create(GoldResearchRequest())
    service.execute(queued.run_id)
    run = service.get(queued.run_id)
    assert run.status == "FAILED"
    assert "stance conflicts" in run.error
    assert gateway.roles.count("orchestrator") == 2


class AuditingGoldGateway(FakeGoldGateway):
    def __init__(self, decisions: list[str]) -> None:
        super().__init__()
        self.decisions = list(decisions)
        self.audit_inputs = []
        self.revisions = []

    def complete(self, role, messages):
        inputs = json.loads(next(message.content for message in messages if message.role == "user"))
        if inputs.get("task") == "final_advice_audit":
            self.roles.append(role)
            self.audit_inputs.append(inputs)
            decision = self.decisions[len(self.audit_inputs) - 1]
            return LLMResponse(
                content=json.dumps(
                    {
                        "decision": decision,
                        "unsupported_claims": []
                        if decision == "PASS"
                        else ["结论需要收缩，不能将历史涨幅外推。"],
                        "missing_risks": [],
                        "confidence_adjustment": 0 if decision == "PASS" else -0.15,
                        "rationale": "仅按冻结事实审查草案。",
                    }
                ),
                provider="fake",
                model="fake-critic",
                input_tokens=100,
                output_tokens=50,
                raw_request_id=f"audit-{len(self.audit_inputs)}",
            )
        if inputs.get("task") == "revise_investment_advice":
            self.revisions.append(inputs)
            self.responses["orchestrator"]["investment_advice"]["summary"] = (
                "长期证据不足，收缩观点并等待，不新增配置。"
            )
        return super().complete(role, messages)


@pytest.mark.parametrize("decisions", [["REJECT"], ["REVISE", "REJECT"], ["REVISE", "REVISE"]])
def test_final_auditor_can_withhold_advice_without_discarding_research(
    tmp_path: Path, decisions: list[str]
) -> None:
    gateway = AuditingGoldGateway(decisions)
    service = _service(tmp_path, gateway=gateway)
    queued = service.create(GoldResearchRequest())
    service.execute(queued.run_id)
    run = service.get(queued.run_id)
    assert run.status == "COMPLETED", run.error
    assert run.result["investment_advice"] is None
    assert run.result["advice_audit"]["status"] == "REJECT"
    assert len(run.result["advice_audit"]["rounds"]) == len(decisions)
    assert run.result["outcome"] == "OBSERVE"
    assert len(gateway.revisions) == (1 if decisions[0] == "REVISE" else 0)


def test_final_audit_is_isolated_and_revised_advice_is_reaudited(tmp_path: Path) -> None:
    gateway = AuditingGoldGateway(["REVISE", "PASS"])
    service = _service(tmp_path, gateway=gateway)
    queued = service.create(GoldResearchRequest())
    service.execute(queued.run_id)
    run = service.get(queued.run_id)
    assert run.status == "COMPLETED", run.error
    assert run.result["advice_audit"]["status"] == "PASS"
    assert run.result["investment_advice"]["summary"] == "长期证据不足，收缩观点并等待，不新增配置。"
    assert len(gateway.audit_inputs) == 2
    for inputs in gateway.audit_inputs:
        assert inputs["analysis_context"]["snapshot_id"] == run.snapshot_id
        assert "draft_advice" in inputs
        assert (
            not {
                "proposal",
                "critic",
                "statistician",
                "local_gates",
                "prior_audit",
                "research_states",
                "deliberation",
                "disagreements",
                "revisions",
            }
            & inputs.keys()
        )
    rounds = run.result["advice_audit"]["rounds"]
    assert rounds[0]["draft_hash"] != rounds[1]["draft_hash"]
    assert rounds[-1]["draft_hash"] == run.result["advice_audit"]["published_draft_hash"]
    assert hash_json(run.result["investment_advice"]) == run.result["advice_audit"]["published_draft_hash"]
    assert any(call["role"] == "final_auditor" for call in run.result["calls"])


@pytest.mark.parametrize(
    "mode", ["provider_error", "same_model", "malformed", "contradictory_pass", "budget"]
)
def test_failed_or_incomplete_audit_never_publishes_a_draft(tmp_path: Path, mode: str) -> None:
    class UnavailableAuditGateway(FakeGoldGateway):
        def complete(self, role, messages):
            if role != "final_auditor":
                return super().complete(role, messages)
            if mode == "provider_error":
                self.roles.append(role)
                raise QuantSystemError("test audit provider unavailable")
            response = super().complete(role, messages)
            if mode == "same_model":
                return response.model_copy(update={"model": "fake-orchestrator"})
            if mode == "malformed":
                return response.model_copy(update={"content": "not json"})
            if mode == "contradictory_pass":
                payload = json.loads(response.content)
                payload["missing_risks"] = ["Risk remains unresolved"]
                return response.model_copy(update={"content": json.dumps(payload)})
            return response

    gateway = UnavailableAuditGateway()
    config = _config(**({"max_llm_calls_per_run": 4} if mode == "budget" else {}))
    service = _service(tmp_path, config=config, gateway=gateway)
    queued = service.create(GoldResearchRequest())
    service.execute(queued.run_id)
    run = service.get(queued.run_id)
    assert run.status == "COMPLETED", run.error
    assert run.result["investment_advice"] is None
    assert run.result["advice_audit"]["status"] == "UNAVAILABLE"
    assert run.result["advice_audit"]["published_draft_hash"] is None
    assert run.result["local_gates"]["最终建议独立终审通过"] is False
    assert run.result["evaluation"]
    assert run.report_artifact_id
    assert gateway.roles.count("final_auditor") == (
        0 if mode == "budget" else 2 if mode in {"malformed", "contradictory_pass"} else 1
    )
    assert service.status()["active_run_id"] is None


@pytest.mark.parametrize("mode", ["missing_role", "same_model"])
def test_final_auditor_must_be_configured_as_a_different_model(tmp_path: Path, mode: str) -> None:
    config = _config().model_dump()
    if mode == "missing_role":
        del config["roles"]["final_auditor"]
    else:
        config["roles"]["final_auditor"]["model"] = config["roles"]["orchestrator"]["model"]
    service = _service(tmp_path, config=AgentLLMConfig.model_validate(config))
    assert service.status()["ready"] is False
    with pytest.raises(QuantSystemError, match="终审"):
        service.create(GoldResearchRequest())


class DeliberatingGoldGateway(AuditingGoldGateway):
    def __init__(self) -> None:
        super().__init__(["REVISE", "PASS"])
        self.inputs = []

    def complete(self, role, messages):
        payload = json.loads(next(message.content for message in messages if message.role == "user"))
        self.inputs.append((role, payload))
        if not role.endswith("_analyst"):
            return super().complete(role, messages)
        self.roles.append(role)
        state = {
            "analyst": role,
            "view": "BEARISH" if role == "risk_analyst" else "BULLISH",
            "confidence": 0.6,
            "summary": "Only frozen price evidence",
            "claims": [
                {
                    "claim": "Historical trend only",
                    "evidence_ids": ["gold_return_126"],
                    "counterevidence_ids": ["independent_oos"],
                    "confidence": 0.5,
                }
            ],
            "risks": ["No macro evidence"],
            "open_questions": [],
            "invalidating_conditions": [],
            "missing_evidence": [],
        }
        if payload["task"] == "analyst_revision":
            state.update(
                view="INSUFFICIENT_EVIDENCE", confidence=0.3, missing_evidence=["real_yield"], claims=[]
            )
            content = {
                "action": "RETRACT",
                "state": state,
                "addressed_disagreement_ids": ["D1"],
                "evidence_requests": ["real_yield"],
                "rationale": "无法从冻结证据确认利率环境下的趋势，撤回方向判断。",
            }
        else:
            content = state
        return LLMResponse(
            content=json.dumps(content),
            provider="fake",
            model=f"fake-{role}",
            input_tokens=100,
            output_tokens=50,
        )


def test_full_revision_chain_is_bounded_and_auditor_never_receives_deliberation(tmp_path: Path) -> None:
    gateway = DeliberatingGoldGateway()
    service = _service(tmp_path, gateway=gateway)
    queued = service.create(GoldResearchRequest())
    assert queued.protocol == "gold-deliberation-v4"
    service.execute(queued.run_id)
    run = service.get(queued.run_id)
    assert run.status == "COMPLETED", run.error
    review = run.result["deliberation"]
    assert len(review["revisions"]) == 2
    assert review["initial_states"][0]["view"] == "BULLISH"
    assert review["states"][0]["view"] == "INSUFFICIENT_EVIDENCE"
    assert review["conclusion"] == "INSUFFICIENT_EVIDENCE"
    assert len(run.result["calls"]) == 9
    assert run.result["advice_audit"]["status"] == "PASS"
    assert run.result["investment_advice"]["stance"] == "WAIT"
    for role, inputs in gateway.inputs:
        if inputs.get("task") == "independent_analysis":
            assert set(inputs) == {"task", "request", "evidence_package"}
        elif inputs.get("task") == "analyst_revision":
            assert len(inputs["peer_states"]) == 1
            assert inputs["peer_states"][0]["analyst"] != role
        elif role == "final_auditor":
            assert not {"deliberation", "research_states", "disagreements", "peer_states"} & inputs.keys()
    stages = {event.stage for event in service.runtime.events(run.run_id)}
    assert {"analyst_revision", "advice_revision", "final_reaudit"} <= stages


def test_insufficient_call_budget_skips_optional_review_without_manufacturing_consensus(
    tmp_path: Path,
) -> None:
    gateway = DeliberatingGoldGateway()
    gateway.decisions = ["PASS"]
    service = _service(tmp_path, gateway=gateway, config=_config(max_llm_calls_per_run=6))
    queued = service.create(GoldResearchRequest())
    service.execute(queued.run_id)
    run = service.get(queued.run_id)
    assert run.status == "COMPLETED", run.error
    assert run.result["deliberation"]["review_status"] == "BUDGET_SKIPPED"
    assert run.result["deliberation"]["revisions"] == []
    assert run.result["investment_advice"]["stance"] == "WAIT"
    assert len(gateway.roles) == 5


def test_concurrent_call_reservations_never_exceed_the_token_budget(tmp_path: Path) -> None:
    gateway = FakeGoldGateway()
    service = _service(tmp_path, config=_config(max_total_tokens_per_run=25000), gateway=gateway)
    barrier = Barrier(2, timeout=5)
    lock = Lock()
    reserved = []
    original = gateway.complete

    def complete(role, messages):
        with lock:
            usage = service.runtime.get(queued.run_id).usage
            reserved.append((usage["tokens_used"], usage["tokens_reserved"]))
            synchronize = len(reserved) <= 2
        if synchronize:
            barrier.wait()
        return original(role, messages)

    gateway.complete = complete
    queued = service.create(GoldResearchRequest())
    service.execute(queued.run_id)
    run = service.get(queued.run_id)
    if run.status == "FAILED":
        assert "budget" in run.error
    else:
        assert run.status == "COMPLETED", run.error
    assert all(used + pending <= service.max_tokens for used, pending in reserved)
    assert len(reserved) >= 2
    assert service.runtime.get(queued.run_id).usage["tokens_reserved"] == 0


def test_analyst_format_repair_is_local_and_each_paid_attempt_is_counted(tmp_path: Path) -> None:
    class RepairGateway(FakeGoldGateway):
        def complete(self, role, messages):
            response = super().complete(role, messages)
            if role == "technical_analyst" and len(messages) == 2:
                return response.model_copy(update={"content": "invalid json"})
            return response

    gateway = RepairGateway()
    service = _service(tmp_path, gateway=gateway)
    queued = service.create(GoldResearchRequest())
    service.execute(queued.run_id)
    run = service.get(queued.run_id)
    assert run.status == "COMPLETED", run.error
    assert gateway.roles.count("technical_analyst") == 2
    assert len(run.result["calls"]) == 6
    report = service.gold.store.get_json(service.gold.registry.get(run.report_artifact_id).uri)
    assert report["tokens_used"] == 900
    attempts = [entry for entry in report["audit"] if entry["role"] == "technical_analyst"]
    assert attempts[0]["parse_error"]
    assert [message["role"] for message in attempts[1]["messages"]] == ["system", "user", "assistant", "user"]


def test_gold_tasks_separate_acquisition_from_required_frozen_snapshot(tmp_path: Path) -> None:
    gateway = FakeGoldGateway()
    service = _service(tmp_path, gateway=gateway)
    collect, research = service.tasks()
    assert collect.descriptor.task_type == "gold.collect"
    assert collect.descriptor.acquisition is True
    assert collect.descriptor.roles == {}
    assert collect.output_fields == {"snapshot_id": str}
    assert collect.request_type.model_validate({}) == GoldResearchRequest()
    assert research.descriptor.task_type == "gold.research"
    assert research.descriptor.acquisition is False
    assert research.descriptor.roles == {role: role for role in ROLES}
    assert research.descriptor.default_budget.max_calls == 12
    assert research.descriptor.default_budget.max_tokens == 64000
    for payload in ({}, {"snapshot_id": 1}, {"snapshot_id": ""}, {"snapshot_id": "x", "unknown": True}):
        with pytest.raises(ValidationError):
            GoldTaskRequest.model_validate(payload)
    with pytest.raises(ValidationError):
        collect.request_type.model_validate({"snapshot_id": "not-a-collect-input"})
    run = service.runtime.submit(RunRequest(task_type="gold.collect"))
    service.runtime.execute(run.run_id)
    completed = service.runtime.get(run.run_id)
    assert completed.status == "COMPLETED", completed.error
    assert gateway.roles == []
    assert completed.usage["calls_used"] == 0
    artifact = service.gold.registry.get(completed.result.payload["snapshot_id"])
    reference = next(ref for ref in completed.result.evidence if ref.artifact_id == artifact.artifact_id)
    assert reference.content_hash == artifact.content_hash
    assert reference.kind == "FACT"
    assert reference.available_at >= artifact.created_at
    assert completed.knowledge_cutoff >= reference.available_at


def test_frozen_snapshot_research_never_refreshes_or_substitutes_the_latest(tmp_path: Path) -> None:
    gateway = FakeGoldGateway()
    service = _service(tmp_path, gateway=gateway)
    collect = service.runtime.submit(RunRequest(task_type="gold.collect"))
    service.runtime.execute(collect.run_id)
    snapshot_id = service.runtime.get(collect.run_id).result.payload["snapshot_id"]
    first = service.gold.load_snapshot(snapshot_id)
    newer = service.gold.publish(first.model_copy(update={"warnings": ["newer snapshot"]}), {"newer": True})
    assert newer.artifact_id != snapshot_id

    def forbidden_fetch(as_of, now):
        pytest.fail("frozen research must not use the network")

    service.fetcher = forbidden_fetch
    run = service.runtime.submit(RunRequest(task_type="gold.research", payload={"snapshot_id": snapshot_id}))
    service.execute(run.run_id)
    result = service.get(run.run_id)
    assert result.status == "COMPLETED", result.error
    assert result.snapshot_id == snapshot_id
    assert result.result["analysis_context"]["as_of"] == first.quotes[0].points[-1].date.isoformat()
    report = service.gold.registry.get(result.report_artifact_id)
    original = service.gold.registry.get(snapshot_id)
    assert {snapshot_id, original.data_snapshot_id} <= set(report.parent_artifact_ids)
    assert newer.artifact_id not in report.parent_artifact_ids
    shared = service.runtime.get(run.run_id)
    assert shared.result.publication.decision == "PASS"
    assert shared.result.publication.content_hash == report.metadata["approved_publication_hash"]
    assert shared.result.publication.content_hash == hash_json(result.result["investment_advice"])
    assert all(ref.kind == "FACT" and ref.artifact_id != report.artifact_id for ref in shared.result.evidence)
    assert any(
        service.gold.registry.get(parent).artifact_type == ArtifactType.RESEARCH_RUN
        for parent in report.parent_artifact_ids
    )
    service.execute(run.run_id)
    assert len(gateway.roles) == 5
    assert len(service.runtime.calls(run.run_id)) == 5


def test_shared_runtime_excludes_other_roots_and_concurrent_execute_is_once(tmp_path: Path) -> None:
    started = Event()
    release = Event()

    class BlockingGateway(FakeGoldGateway):
        def complete(self, role, messages):
            if role == "technical_analyst":
                started.set()
                assert release.wait(5)
            return super().complete(role, messages)

    gateway = BlockingGateway()
    service = _service(tmp_path, gateway=gateway)
    registry = ModuleRegistry()
    for task in service.tasks():
        registry.register(task)
    registry.register(
        ResearchTask(
            TaskDescriptor(task_type="other.observe", module_id="other", title="Other research"),
            EmptyRequest,
            lambda ctx, request: TaskResult(outcome="OBSERVE", summary="Other result"),
        )
    )
    runtime = ResearchRuntime(
        registry,
        ArtifactRunRepository(service.gold.store, service.gold.registry),
        service.config,
        gateway,
        credentials_required=False,
    )
    service.bind_runtime(runtime)
    other = runtime.submit(RunRequest(task_type="other.observe"))
    assert service.status()["active_run_id"] == other.run_id
    assert service.status()["active_gold_run_id"] is None
    assert service.status()["busy"] is True
    assert service.list_runs() == []
    with pytest.raises(GoldResearchBusy):
        service.create(GoldResearchRequest())
    with pytest.raises(ArtifactNotFoundError):
        service.get(other.run_id)
    runtime.execute(other.run_id)
    run = service.create(GoldResearchRequest())
    assert service.status()["active_gold_run_id"] == run.run_id
    with pytest.raises(ResearchConflict):
        runtime.submit(RunRequest(task_type="other.observe"))
    with ThreadPoolExecutor(max_workers=2) as workers:
        execution = workers.submit(service.execute, run.run_id)
        try:
            assert started.wait(5)
            active = service.get(run.run_id)
            assert active.status == "RUNNING"
            assert active.stage == "independent_analysis"
            workers.submit(service.execute, run.run_id).result(timeout=5)
        finally:
            release.set()
        execution.result(timeout=10)
    assert service.get(run.run_id).status == "COMPLETED"
    assert len(gateway.roles) == 5
    receipts = runtime.calls(run.run_id)
    assert [receipt.sequence for receipt in receipts] == list(range(1, 6))
    assert runtime.get(run.run_id).usage["calls_used"] == 5
    assert service.status()["active_run_id"] is None


def test_gold_and_another_task_charge_the_same_root_budget(tmp_path: Path) -> None:
    gateway = FakeGoldGateway()
    service = _service(tmp_path, gateway=gateway)

    def spend(ctx, request):
        from quant_system.agents.llm import LLMMessage

        ctx.call("orchestrator", [LLMMessage(role="user", content="{}")], dict, purpose="other-research")
        return TaskResult(outcome="OBSERVE", summary="One shared call")

    service.runtime.module_registry.register(
        ResearchTask(
            TaskDescriptor(
                task_type="other.spend",
                module_id="other",
                title="Other call",
                roles={"orchestrator": "orchestrator"},
            ),
            EmptyRequest,
            spend,
        )
    )
    budget = BudgetLimits(max_calls=4, max_tokens=64000)
    run = service.runtime.submit(
        RunRequest(
            plan=ResearchPlan(
                tasks=[
                    PlanTask(task_id="collect", task_type="gold.collect", budget=budget),
                    PlanTask(task_id="other", task_type="other.spend", depends_on=["collect"], budget=budget),
                    PlanTask(
                        task_id="gold",
                        task_type="gold.research",
                        depends_on=["collect", "other"],
                        budget=budget,
                        inputs={"snapshot_id": InputRef(task_id="collect", output_key="snapshot_id")},
                    ),
                ],
                budget=budget,
            ),
            budget=budget,
        )
    )
    service.execute(run.run_id)
    failed = service.get(run.run_id)
    assert failed.status == "FAILED"
    assert "budget" in failed.error
    assert len(gateway.roles) == 4
    assert len(service.runtime.calls(run.run_id)) == 4
    assert service.runtime.get(run.run_id).usage["calls_used"] == 4


def test_legacy_history_is_read_only_until_explicit_recovery(tmp_path: Path) -> None:
    gateway = FakeGoldGateway()
    service = _service(tmp_path, gateway=gateway)
    now = datetime.now(UTC)
    original = GoldResearchRun(
        run_id="legacy-gold",
        protocol="gold-deliberation-v4",
        created_at=now,
        updated_at=now,
        request=GoldResearchRequest(),
        status="RUNNING",
        stage="independent_analysis",
    )
    payload = {
        "run": original.model_dump(mode="json"),
        "configuration": {},
        "audit": [],
        "tokens_used": 0,
        "revision": 1,
    }
    uri, digest = service.gold.store.put_content_addressed_json("gold/research/runs", payload)
    artifact = service.gold.registry.register(
        ArtifactEnvelope(
            artifact_type=ArtifactType.GOLD_RESEARCH_RUN,
            producer="legacy-gold",
            uri=uri,
            content_hash=digest,
            metadata={"run_id": original.run_id, "revision": 1, "status": "RUNNING", "stage": original.stage},
        )
    )
    assert service.get(original.run_id).status == "FAILED"
    assert service.list_runs()[0].run_id == original.run_id
    service.execute(original.run_id)
    assert gateway.roles == []
    assert len(service.gold.registry.find_by_type(ArtifactType.GOLD_RESEARCH_RUN)) == 1
    service.recover_legacy()
    revisions = service.gold.registry.find_by_type(ArtifactType.GOLD_RESEARCH_RUN)
    assert len(revisions) == 2
    derived = next(item for item in revisions if item.artifact_id != artifact.artifact_id)
    assert artifact.artifact_id in derived.parent_artifact_ids
    assert service.gold.store.get_json(artifact.uri) == payload
    assert hash_bytes(service.gold.store.get_bytes(artifact.uri)) == digest
    service.recover_legacy()
    assert len(service.gold.registry.find_by_type(ArtifactType.GOLD_RESEARCH_RUN)) == 2
    assert service.gold.registry.find_by_type(ArtifactType.GOLD_RESEARCH_REPORT) == []


def _macro_fixture(service: GoldResearchService, *, available_at=None, kind="FACT"):
    now = datetime.now(UTC)
    uri, digest = service.gold.store.put_content_addressed_json(
        "test/macro", {"as_of": "2000-01-01", "kind": kind}
    )
    artifact = service.gold.registry.register(
        ArtifactEnvelope(
            artifact_type=ArtifactType.MACRO_SNAPSHOT,
            producer="test-macro",
            uri=uri,
            content_hash=digest,
            created_at=now,
            metadata={"available_at": (available_at or now).isoformat(), "evidence_kind": kind},
        )
    )
    reference = EvidenceRef(
        artifact_id=artifact.artifact_id,
        content_hash=artifact.content_hash,
        namespace="macro",
        kind=kind,
        available_at=available_at or now,
    )
    return artifact, {
        "snapshot_id": artifact.artifact_id,
        "items": {
            "real_yield": {
                "category": "macro",
                "value": 1.5,
                "definition": "Observed real yield",
                "source": "test-macro",
            }
        },
        "evidence": [reference.model_dump(mode="json")],
    }


def test_macro_collection_injects_facts_and_partial_coverage_keeps_wait(tmp_path: Path) -> None:
    calls = []

    def reader(snapshot_id, cutoff):
        calls.append((snapshot_id, cutoff))
        return macro_payload

    service = _service(tmp_path, macro_evidence_reader=reader)
    macro, macro_payload = _macro_fixture(service)
    runtime = service.runtime
    runtime.module_registry.register(
        ResearchTask(
            TaskDescriptor(
                task_type="macro.collect", module_id="macro", title="Macro collection", acquisition=True
            ),
            EmptyRequest,
            lambda ctx, request: TaskResult(
                outcome="COLLECTED",
                summary="Frozen macro facts",
                payload={"snapshot_id": macro.artifact_id},
                evidence=[ctx.evidence(EvidenceRef.model_validate(macro_payload["evidence"][0]))],
            ),
            output_fields={"snapshot_id": str},
        )
    )
    run = runtime.submit(
        RunRequest(
            plan=ResearchPlan(
                tasks=[
                    PlanTask(task_id="gold_collect", task_type="gold.collect"),
                    PlanTask(task_id="macro_collect", task_type="macro.collect"),
                    PlanTask(
                        task_id="research",
                        task_type="gold.research",
                        depends_on=["gold_collect", "macro_collect"],
                        inputs={
                            "snapshot_id": InputRef(task_id="gold_collect", output_key="snapshot_id"),
                            "macro_snapshot_id": InputRef(task_id="macro_collect", output_key="snapshot_id"),
                        },
                    ),
                ]
            )
        )
    )
    service.execute(run.run_id)
    result = service.get(run.run_id)
    assert result.status == "COMPLETED", result.error
    assert result.result["investment_advice"]["stance"] == "WAIT"
    assert result.result["local_gates"]["独立审议证据充分"] is False
    assert result.result["deliberation"]["conclusion"] == "INSUFFICIENT_EVIDENCE"
    assert result.result["outcome"] == "OBSERVE"
    assert result.result["deployment_eligible"] is False
    context = result.result["analysis_context"]
    assert context["macro_snapshot_id"] == macro.artifact_id
    assert set(context["facts"]["missing_macro_categories"]) == {
        key
        for key, item in result.result["evidence_package"]["items"].items()
        if item["category"] == "macro" and item["value"] is None
    }
    assert context["as_of"] != "2000-01-01"
    assert result.result["evidence_package"]["items"]["real_yield"]["value"] == 1.5
    assert "macro_analyst" in service.gateway.roles
    shared = runtime.get(run.run_id)
    assert calls == [(macro.artifact_id, shared.knowledge_cutoff)]
    assert macro.artifact_id in {
        ref["artifact_id"] for ref in shared.usage["evidence_manifest"]["references"]
    }
    report = service.gold.registry.get(result.report_artifact_id)
    assert macro.artifact_id in report.parent_artifact_ids
    assert all(ref["kind"] == "FACT" for ref in shared.usage["evidence_manifest"]["references"])


@pytest.mark.parametrize(
    "mode", ["future", "forged_time", "opinion", "wrong_snapshot", "agent_opinions", "missing_reader"]
)
def test_macro_refs_cannot_smuggle_future_data_or_agent_opinions(tmp_path: Path, mode: str) -> None:
    calls = []

    def reader(snapshot_id, cutoff):
        calls.append((snapshot_id, cutoff))
        return macro_payload

    service = _service(tmp_path, macro_evidence_reader=None if mode == "missing_reader" else reader)
    future = datetime.now(UTC) + timedelta(days=1) if mode in {"future", "forged_time"} else None
    macro, macro_payload = _macro_fixture(
        service, available_at=future, kind="OPINION" if mode == "opinion" else "FACT"
    )
    if mode == "forged_time":
        macro_payload["evidence"][0]["available_at"] = macro.created_at.isoformat()
    elif mode == "wrong_snapshot":
        macro_payload["snapshot_id"] = "different-snapshot"
    elif mode == "agent_opinions":
        macro_payload["research_states"] = [{"summary": "Another agent opinion is not a fact"}]
    collected = service.runtime.submit(RunRequest(task_type="gold.collect"))
    service.runtime.execute(collected.run_id)
    snapshot_id = service.runtime.get(collected.run_id).result.payload["snapshot_id"]
    run = service.runtime.submit(
        RunRequest(
            task_type="gold.research",
            payload={
                "snapshot_id": snapshot_id,
                "macro_snapshot_id": macro.artifact_id,
            },
        )
    )
    service.execute(run.run_id)
    failed = service.get(run.run_id)
    assert failed.status == "FAILED"
    assert service.gateway.roles == []
    assert failed.report_artifact_id is None
    if calls:
        assert calls[0][0] == macro.artifact_id
        assert calls[0][1] == service.runtime.get(run.run_id).knowledge_cutoff
    if mode == "future":
        assert "knowledge cutoff" in failed.error
    elif mode == "forged_time":
        assert "availability predates" in failed.error


def test_snapshot_created_after_historical_cutoff_fails_before_llm(tmp_path: Path) -> None:
    service = _service(tmp_path)
    collected = service.runtime.submit(RunRequest(task_type="gold.collect"))
    service.runtime.execute(collected.run_id)
    snapshot_id = service.runtime.get(collected.run_id).result.payload["snapshot_id"]
    run = service.runtime.submit(
        RunRequest(
            task_type="gold.research",
            payload={"snapshot_id": snapshot_id},
            mode="HISTORICAL",
            knowledge_cutoff=datetime(2020, 1, 1, tzinfo=UTC),
        )
    )
    service.execute(run.run_id)
    assert service.get(run.run_id).status == "FAILED"
    assert "knowledge cutoff" in service.get(run.run_id).error
    assert service.gateway.roles == []


def test_cancellation_before_publish_withholds_report_and_paid_call_replay(tmp_path: Path) -> None:
    class CancellingGateway(FakeGoldGateway):
        def complete(self, role, messages):
            response = super().complete(role, messages)
            if role == "final_auditor":
                service.runtime.cancel(run.run_id)
            return response

    gateway = CancellingGateway()
    service = _service(tmp_path, gateway=gateway)
    run = service.create(GoldResearchRequest())
    service.execute(run.run_id)
    assert service.runtime.get(run.run_id).status == "CANCELLED"
    failed = service.get(run.run_id)
    assert failed.status == "FAILED"
    assert failed.stage == "failed"
    assert failed.result is None
    assert failed.report_artifact_id is None
    assert service.gold.registry.find_by_type(ArtifactType.GOLD_RESEARCH_REPORT) == []
    service.execute(run.run_id)
    assert len(gateway.roles) == 5


def test_report_audit_projects_failed_shared_receipts_and_withheld_publication(tmp_path: Path) -> None:
    class FailedAuditGateway(FakeGoldGateway):
        def complete(self, role, messages):
            if role == "final_auditor":
                self.roles.append(role)
                raise QuantSystemError("Audit provider unavailable")
            return super().complete(role, messages)

    service = _service(tmp_path, gateway=FailedAuditGateway())
    run = service.create(GoldResearchRequest())
    service.execute(run.run_id)
    projected = service.get(run.run_id)
    assert projected.status == "COMPLETED", projected.error
    report = service.gold.store.get_json(service.gold.registry.get(projected.report_artifact_id).uri)
    failed = report["audit"][-1]
    assert failed["role"] == "final_auditor"
    assert failed["purpose"] == failed["task"] == "final_advice_audit"
    assert failed["status"] == "FAILED"
    assert failed["error"] == "Audit provider unavailable"
    assert failed["response"] is None
    assert failed["messages"]
    assert report["tokens_used"] == service.runtime.get(run.run_id).usage["tokens_used"]
    research = next(
        service.runtime.get(child)
        for child in service.runtime.get(run.run_id).children
        if service.runtime.get(child).task_type == "gold.research"
    )
    assert research.result.publication.decision == "WITHHELD"
