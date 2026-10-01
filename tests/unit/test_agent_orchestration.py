from __future__ import annotations

import json
from pathlib import Path

from quant_system.agents.config import AgentLLMConfig
from quant_system.agents.llm import LLMResponse
from quant_system.agents.orchestration import (
    AgentResearchOrchestrator,
    AgentResearchRequest,
    CriticReview,
    FactorCandidate,
    OrchestratorDecision,
    StatisticalReview,
    _parse_structured,
)
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.domain.enums import ArtifactType, ExperimentState
from quant_system.factors.evaluation import FactorLab
from quant_system.infra.db import create_all, make_engine, make_session_factory
from quant_system.research.experiments import ExperimentService


class FakeGateway:
    def __init__(self) -> None:
        self.roles: list[str] = []
        self.responses = {
            "researcher": {
                "name": "20日动量",
                "hypothesis": "中期价格趋势在横截面上具有延续性",
                "expression": "rank(delta(close, 20))",
                "horizon": 5,
            },
            "critic": {"approved": True, "issues": [], "rationale": "PIT-safe baseline"},
            "statistician": {"robust": True, "findings": ["coverage sufficient"], "rationale": "usable"},
            "orchestrator": {
                "recommend_candidate": True,
                "rationale": "local gates and reviews passed",
                "next_steps": ["paper validation"],
            },
        }

    def complete(self, role, messages):
        self.roles.append(role)
        return LLMResponse(
            content=json.dumps(self.responses[role], ensure_ascii=False),
            provider="fake",
            model=f"fake-{role}",
            input_tokens=10,
            output_tokens=5,
            raw_request_id=f"request-{role}",
        )


class RepairingGateway(FakeGateway):
    def __init__(self) -> None:
        super().__init__()
        self.researcher_calls = 0

    def complete(self, role, messages):
        if role == "researcher":
            self.researcher_calls += 1
            if self.researcher_calls == 1:
                self.responses["researcher"] = {
                    "name": "invalid first draft",
                    "hypothesis": "repair exercise",
                    "expression": "rank(close, 5)",
                    "horizon": 1,
                }
            else:
                self.responses["researcher"] = {
                    "name": "repaired momentum",
                    "hypothesis": "trend persistence",
                    "expression": "rank(delta(close, 20))",
                    "horizon": 5,
                }
        return super().complete(role, messages)


class SchemaRepairingGateway(FakeGateway):
    def __init__(self) -> None:
        super().__init__()
        self.statistician_calls = 0

    def complete(self, role, messages):
        if role == "statistician":
            self.statistician_calls += 1
            if self.statistician_calls == 1:
                self.responses["statistician"] = {
                    "robustness": "good",
                    "findings": {"rank_ic": 0.04},
                    "rationale": "wrong schema",
                }
            else:
                self.responses["statistician"] = {
                    "robust": True,
                    "findings": ["coverage sufficient"],
                    "rationale": "schema repaired without changing judgment",
                }
        return super().complete(role, messages)


def _config() -> AgentLLMConfig:
    return AgentLLMConfig.model_validate({
        "providers": {
            "fake": {
                "kind": "openai_compatible",
                "api_key_env": "FAKE_KEY",
                "base_url": "https://example.invalid",
            }
        },
        "roles": {
            role: {"provider": "fake", "model": f"fake-{role}"}
            for role in ("researcher", "critic", "statistician", "orchestrator")
        },
        "factor_validation": {
            "min_ic_days": 1,
            "min_coverage": 0,
            "min_abs_rank_ic": 0,
        },
    })


def test_structured_response_accepts_json_code_fence() -> None:
    candidate = _parse_structured(
        '```json\n{"name":"Momentum","hypothesis":"Trend","expression":"rank(close)","horizon":"1d"}\n```',
        FactorCandidate,
    )
    assert candidate.expression == "rank(close)"
    assert candidate.horizon == 1

    target_as_horizon = _parse_structured(
        '{"name":"Momentum","hypothesis":"Trend","expression":"rank(close)",'
        '"horizon":"NEXT_OPEN_TO_CLOSE"}',
        FactorCandidate,
    )
    assert target_as_horizon.horizon == 1


def test_structured_response_normalizes_single_list_items() -> None:
    critic = _parse_structured(
        '{"approved":false,"issues":"未来数据风险","rationale":"拒绝"}',
        CriticReview,
    )
    statistician = _parse_structured(
        '{"robust":false,"findings":"IC 不稳定","rationale":"证据不足"}',
        StatisticalReview,
    )
    decision = _parse_structured(
        '{"recommend_candidate":false,"rationale":"门槛未通过",'
        '"next_steps":"扩大样本"}',
        OrchestratorDecision,
    )

    assert critic.issues == ["未来数据风险"]
    assert statistician.findings == ["IC 不稳定"]
    assert decision.next_steps == ["扩大样本"]


def test_four_agent_loop_runs_deterministic_evaluation_and_persists_report(
    tmp_path: Path, provider
) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'agents.db'}")
    create_all(engine)
    session_factory = make_session_factory(engine)
    gateway = FakeGateway()
    registry = ArtifactRegistry(session_factory)
    experiments = ExperimentService(session_factory)
    orchestrator = AgentResearchOrchestrator(
        config=_config(),
        gateway=gateway,
        experiments=experiments,
        factor_lab=FactorLab(session_factory, LocalParquetProvider(provider.data_dir.as_posix())),
        registry=registry,
    )

    result = orchestrator.run(AgentResearchRequest(
        topic="寻找一个可解释的中期量价因子",
        start_date="2024-02-01",
        end_date="2024-05-31",
    ))

    assert gateway.roles == ["researcher", "critic", "statistician", "orchestrator"]
    assert result.experiment.state == ExperimentState.CANDIDATE
    assert result.factor_run.status == "COMPLETED"
    assert result.factor_run.metrics["n_ic_days"] > 0
    assert all(result.local_gates.values())
    assert experiments.runs(result.experiment.experiment_id)[0].status == "CANDIDATE"
    assert registry.get(result.report_artifact_id).artifact_type == ArtifactType.RESEARCH_REPORT


def test_critic_rejection_still_completes_auditable_four_agent_loop(
    tmp_path: Path, provider
) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'rejected.db'}")
    create_all(engine)
    session_factory = make_session_factory(engine)
    gateway = FakeGateway()
    gateway.responses["critic"] = {
        "approved": False,
        "issues": ["economic rationale is weak"],
        "rationale": "reject but retain deterministic evidence",
    }
    orchestrator = AgentResearchOrchestrator(
        config=_config(),
        gateway=gateway,
        experiments=ExperimentService(session_factory),
        factor_lab=FactorLab(session_factory, LocalParquetProvider(provider.data_dir.as_posix())),
        registry=ArtifactRegistry(session_factory),
    )

    result = orchestrator.run(AgentResearchRequest(
        topic="审计被 Critic 否决的候选",
        start_date="2024-02-01",
        end_date="2024-05-31",
    ))

    assert gateway.roles == ["researcher", "critic", "statistician", "orchestrator"]
    assert result.experiment.state == ExperimentState.REJECTED
    assert result.factor_run.status == "COMPLETED"
    assert result.critic.approved is False


def test_researcher_repairs_locally_rejected_dsl_once(tmp_path: Path, provider) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'repair.db'}")
    create_all(engine)
    session_factory = make_session_factory(engine)
    gateway = RepairingGateway()
    orchestrator = AgentResearchOrchestrator(
        config=_config(),
        gateway=gateway,
        experiments=ExperimentService(session_factory),
        factor_lab=FactorLab(session_factory, LocalParquetProvider(provider.data_dir.as_posix())),
        registry=ArtifactRegistry(session_factory),
    )

    result = orchestrator.run(AgentResearchRequest(
        topic="修复非法 DSL 候选",
        start_date="2024-02-01",
        end_date="2024-05-31",
    ))

    assert gateway.roles == [
        "researcher", "researcher", "critic", "statistician", "orchestrator"
    ]
    assert result.candidate.expression == "rank(delta(close, 20))"
    assert result.experiment.state == ExperimentState.CANDIDATE


def test_role_repairs_invalid_response_schema_once(tmp_path: Path, provider) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'schema-repair.db'}")
    create_all(engine)
    session_factory = make_session_factory(engine)
    gateway = SchemaRepairingGateway()
    orchestrator = AgentResearchOrchestrator(
        config=_config(),
        gateway=gateway,
        experiments=ExperimentService(session_factory),
        factor_lab=FactorLab(session_factory, LocalParquetProvider(provider.data_dir.as_posix())),
        registry=ArtifactRegistry(session_factory),
    )

    result = orchestrator.run(AgentResearchRequest(
        topic="修复统计审查响应结构",
        start_date="2024-02-01",
        end_date="2024-05-31",
    ))

    assert gateway.roles == [
        "researcher", "critic", "statistician", "statistician", "orchestrator"
    ]
    assert result.statistician.robust is True
    assert len(result.calls) == 5