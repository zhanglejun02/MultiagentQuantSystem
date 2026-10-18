from __future__ import annotations

import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest

from quant_system.agents.config import AgentLLMConfig
from quant_system.agents.llm import LLMMessage, LLMResponse
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
from quant_system.data.snapshots.service import DataSnapshotService
from quant_system.domain.enums import ArtifactType, ExperimentState
from quant_system.domain.exceptions import QuantSystemError
from quant_system.factors.evaluation import FactorLab
from quant_system.infra.db import create_all, make_engine, make_session_factory
from quant_system.research.experiments import ExperimentService
from quant_system.research.runtime.calls import BudgetLedger, BudgetLimits, StructuredCaller


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
    return AgentLLMConfig.model_validate(
        {
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
        }
    )


def test_structured_response_accepts_json_code_fence() -> None:
    candidate = _parse_structured(
        '```json\n{"name":"Momentum","hypothesis":"Trend","expression":"rank(close)","horizon":"1d"}\n```',
        FactorCandidate,
    )
    assert candidate.expression == "rank(close)"
    assert candidate.horizon == 1

    target_as_horizon = _parse_structured(
        '{"name":"Momentum","hypothesis":"Trend","expression":"rank(close)","horizon":"NEXT_OPEN_TO_CLOSE"}',
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
        '{"recommend_candidate":false,"rationale":"门槛未通过","next_steps":"扩大样本"}',
        OrchestratorDecision,
    )

    assert critic.issues == ["未来数据风险"]
    assert statistician.findings == ["IC 不稳定"]
    assert decision.next_steps == ["扩大样本"]


def test_reviews_run_concurrently_with_independent_local_evidence(tmp_path: Path, provider) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'independent-reviews.db'}")
    create_all(engine)
    sessions = make_session_factory(engine)
    evaluated = Event()
    statistician_finished = Event()
    barrier = Barrier(2, timeout=5)
    context_lock = Lock()
    review_contexts = {}
    review_messages = {}
    measured_metrics = {}

    class ObservedFactorLab(FactorLab):
        def evaluate(self, **kwargs):
            result = super().evaluate(**kwargs)
            measured_metrics.update(result.metrics)
            evaluated.set()
            return result

    class IndependentReviewGateway(FakeGateway):
        def complete(self, role, messages):
            if role in {"critic", "statistician"}:
                assert evaluated.is_set(), "local FactorLab must finish before either review"
                payload = json.loads(messages[-1].content)
                assert payload["candidate"] == {
                    "expression": "rank(delta(close, 20))",
                    "horizon": 5,
                }
                assert payload["metrics"] == measured_metrics
                assert "critic" not in payload
                assert "statistician" not in payload
                context = "\n".join(message.content for message in messages)
                assert "AUTHOR_NARRATIVE_SENTINEL" not in context
                assert "AUTHOR_NAME_SENTINEL" not in context
                assert '"hypothesis"' not in context
                with context_lock:
                    review_contexts[role] = payload
                    review_messages[role] = messages
                barrier.wait()
                if role == "critic":
                    assert statistician_finished.wait(5)
            response = super().complete(role, messages)
            if role == "statistician":
                statistician_finished.set()
            return response

    gateway = IndependentReviewGateway()
    gateway.responses["researcher"]["name"] = "AUTHOR_NAME_SENTINEL"
    gateway.responses["researcher"]["hypothesis"] = "AUTHOR_NARRATIVE_SENTINEL"
    orchestrator = AgentResearchOrchestrator(
        config=_config(),
        gateway=gateway,
        experiments=ExperimentService(sessions),
        factor_lab=ObservedFactorLab(sessions, LocalParquetProvider(provider.data_dir.as_posix())),
        registry=ArtifactRegistry(sessions),
    )

    result = orchestrator.run(
        AgentResearchRequest(
            topic="Independent factor review",
            start_date="2024-02-01",
            end_date="2024-05-31",
        )
    )

    assert gateway.roles == ["researcher", "statistician", "critic", "orchestrator"]
    assert [call.role for call in result.calls] == ["researcher", "critic", "statistician", "orchestrator"]
    assert review_messages["critic"] is not review_messages["statistician"]
    assert review_contexts["critic"]["metrics"]["n_ic_days"] > 0
    assert result.experiment.state == ExperimentState.CANDIDATE


@pytest.mark.parametrize("failed_label", [None, "first"])
def test_recorded_gateway_keeps_parallel_receipts_and_call_indices(
    tmp_path: Path, monkeypatch, capsys, failed_label
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    recorded_gateway = importlib.import_module("run_agent_backtest").RecordedGateway
    barrier = Barrier(2, timeout=5)

    class ConcurrentGateway:
        def complete(self, role, messages):
            barrier.wait()
            if messages[-1].content == failed_label:
                raise QuantSystemError("provider unavailable")
            return LLMResponse(
                content=messages[-1].content,
                provider="fake",
                model=f"fake-{role}",
                raw_request_id=f"request-{messages[-1].content}",
            )

    gateway = recorded_gateway(ConcurrentGateway(), tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(gateway.complete, "critic", [LLMMessage(role="user", content=label)])
            for label in ("first", "second")
        ]
        responses = []
        for label, future in zip(("first", "second"), futures, strict=True):
            if label == failed_label:
                with pytest.raises(QuantSystemError, match="provider unavailable"):
                    future.result()
            else:
                responses.append(future.result())

    assert gateway.count == 2
    assert {response.raw_request_id for response in responses} == {
        f"request-{label}" for label in ("first", "second") if label != failed_label
    }
    assert {path.name for path in tmp_path.iterdir()} == {"01_critic.json", "02_critic.json"}
    output = capsys.readouterr().out.splitlines()
    for path in tmp_path.iterdir():
        receipt = json.loads(path.read_text())
        label = receipt["messages"][-1]["content"]
        index = int(path.name.split("_")[0])
        if label == failed_label:
            assert "response" not in receipt
            assert output.count(f"LLM call {index}: critic failed; request retained") == 1
        else:
            assert receipt["response"]["content"] == label
            assert receipt["response"]["raw_request_id"] == f"request-{label}"
            assert sum(line.startswith(f"LLM call {index}: critic completed ") for line in output) == 1


def test_four_agent_loop_runs_deterministic_evaluation_and_persists_report(tmp_path: Path, provider) -> None:
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

    result = orchestrator.run(
        AgentResearchRequest(
            topic="寻找一个可解释的中期量价因子",
            start_date="2024-02-01",
            end_date="2024-05-31",
        )
    )

    assert gateway.roles[0] == "researcher"
    assert sorted(gateway.roles[1:-1]) == ["critic", "statistician"]
    assert gateway.roles[-1] == "orchestrator"
    assert result.experiment.state == ExperimentState.CANDIDATE
    assert result.factor_run.status == "COMPLETED"
    assert result.factor_run.metrics["n_ic_days"] > 0
    assert all(result.local_gates.values())
    assert experiments.runs(result.experiment.experiment_id)[0].status == "CANDIDATE"
    assert registry.get(result.report_artifact_id).artifact_type == ArtifactType.RESEARCH_REPORT


def test_critic_rejection_still_completes_auditable_four_agent_loop(tmp_path: Path, provider) -> None:
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

    result = orchestrator.run(
        AgentResearchRequest(
            topic="审计被 Critic 否决的候选",
            start_date="2024-02-01",
            end_date="2024-05-31",
        )
    )

    assert gateway.roles[0] == "researcher"
    assert sorted(gateway.roles[1:-1]) == ["critic", "statistician"]
    assert gateway.roles[-1] == "orchestrator"
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

    result = orchestrator.run(
        AgentResearchRequest(
            topic="修复非法 DSL 候选",
            start_date="2024-02-01",
            end_date="2024-05-31",
        )
    )

    assert gateway.roles[:2] == ["researcher", "researcher"]
    assert sorted(gateway.roles[2:-1]) == ["critic", "statistician"]
    assert gateway.roles[-1] == "orchestrator"
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

    result = orchestrator.run(
        AgentResearchRequest(
            topic="修复统计审查响应结构",
            start_date="2024-02-01",
            end_date="2024-05-31",
        )
    )

    assert gateway.roles[0] == "researcher"
    assert sorted(gateway.roles[1:-1]) == ["critic", "statistician", "statistician"]
    assert gateway.roles[-1] == "orchestrator"
    assert result.statistician.robust is True
    assert len(result.calls) == 5


def test_call_budget_stops_before_an_extra_provider_request(tmp_path: Path, provider) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'budget.db'}")
    create_all(engine)
    sessions = make_session_factory(engine)
    config = _config()
    config = config.model_copy(
        update={
            "research_budget": config.research_budget.model_copy(
                update={"max_llm_calls_per_run": 4},
            )
        }
    )
    gateway = SchemaRepairingGateway()
    orchestrator = AgentResearchOrchestrator(
        config=config,
        gateway=gateway,
        experiments=ExperimentService(sessions),
        factor_lab=FactorLab(sessions, LocalParquetProvider(provider.data_dir.as_posix())),
        registry=ArtifactRegistry(sessions),
    )
    with pytest.raises(QuantSystemError, match="call budget exceeded"):
        orchestrator.run(
            AgentResearchRequest(topic="预算边界检查", start_date="2024-02-01", end_date="2024-05-31")
        )
    assert gateway.roles[0] == "researcher"
    assert sorted(gateway.roles[1:]) == ["critic", "statistician", "statistician"]


def test_review_token_budget_is_reserved_together_before_paid_calls(
    session_factory, registry, provider
) -> None:
    config = _config()
    config = config.model_copy(
        update={
            "research_budget": config.research_budget.model_copy(update={"max_total_tokens_per_run": 8500})
        }
    )
    gateway = FakeGateway()
    experiments = ExperimentService(session_factory)
    orchestrator = AgentResearchOrchestrator(
        config=config,
        gateway=gateway,
        experiments=experiments,
        factor_lab=FactorLab(session_factory, provider),
        registry=registry,
    )

    with pytest.raises(QuantSystemError, match="token budget exceeded"):
        orchestrator.run(
            AgentResearchRequest(topic="Reserve both reviews", start_date="2024-02-01", end_date="2024-05-31")
        )

    assert gateway.roles == ["researcher"]


@pytest.mark.parametrize(
    "missing", [("input_tokens",), ("output_tokens",), ("input_tokens", "output_tokens")]
)
def test_missing_usage_keeps_conservative_reservation_before_an_extra_call(
    session_factory, registry, provider, missing
) -> None:
    class MissingUsageGateway(FakeGateway):
        def complete(self, role, messages):
            response = super().complete(role, messages)
            return response.model_copy(update={name: None for name in missing})

    gateway = MissingUsageGateway()
    orchestrator = AgentResearchOrchestrator(
        config=_config(),
        gateway=gateway,
        experiments=ExperimentService(session_factory),
        factor_lab=FactorLab(session_factory, provider),
        registry=registry,
    )
    messages = [LLMMessage(role="user", content="Use deterministic evidence only")]
    reservation = sum(orchestrator._token_limits("researcher", messages))
    ledger = BudgetLedger(BudgetLimits(max_calls=4, max_tokens=reservation + 15))
    caller = StructuredCaller(_config(), gateway, ledger)

    caller.complete("researcher", messages)
    with pytest.raises(QuantSystemError, match="token budget exceeded"):
        caller.complete("critic", messages)

    assert gateway.roles == ["researcher"]
    assert ledger.usage()["tokens_used"] >= reservation
    for name in missing:
        assert getattr(caller.receipts[0].response, name) is None


@pytest.mark.parametrize("budget_kind", ["call", "token"])
def test_concurrent_schema_repairs_cannot_exceed_budget(monkeypatch, budget_kind) -> None:
    barrier = Barrier(2, timeout=5)
    budget_blocked = Event()
    attempt_lock = Lock()
    attempts = {"critic": 0, "statistician": 0}
    reserve = BudgetLedger.reserve

    def observed_reserve(ledger, tokens):
        try:
            return reserve(ledger, tokens)
        except QuantSystemError:
            budget_blocked.set()
            raise

    monkeypatch.setattr(BudgetLedger, "reserve", observed_reserve)

    class RepairBothReviewsGateway(FakeGateway):
        def complete(self, role, messages):
            response = super().complete(role, messages)
            if role in attempts:
                with attempt_lock:
                    attempts[role] += 1
                    attempt = attempts[role]
                if attempt == 1:
                    barrier.wait()
                    return response.model_copy(update={"content": "invalid JSON"})
                assert budget_blocked.wait(5), "competing repair must fail before this call completes"
            return response.model_copy(update={"content": "true"})

    config = _config()
    messages = [LLMMessage(role="user", content="Review local evidence")]
    repair_messages = [
        *messages,
        LLMMessage(role="assistant", content="invalid JSON"),
        LLMMessage(
            role="user",
            content=(
                "Your previous response failed local validation: response is not valid JSON. "
                "Preserve its factual judgment and return JSON only matching this schema exactly: "
                + json.dumps({"type": "boolean"})
            ),
        ),
    ]

    def tokens(request_messages):
        serialized = json.dumps(
            [message.model_dump(mode="json") for message in request_messages], ensure_ascii=False
        )
        return len(serialized.encode("utf-8")) + 256 + config.roles["critic"].max_output_tokens

    token_limit = tokens(messages) + tokens(repair_messages) + 15
    ledger = BudgetLedger(
        BudgetLimits(
            max_calls=3 if budget_kind == "call" else 4,
            max_tokens=200_000 if budget_kind == "call" else token_limit,
        )
    )
    gateway = RepairBothReviewsGateway()
    caller = StructuredCaller(config, gateway, ledger)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(caller.call, role, messages, bool) for role in attempts]
        errors = []
        for future in futures:
            try:
                assert future.result() is True
            except QuantSystemError as exc:
                errors.append(str(exc))

    assert len(errors) == 1
    assert f"{budget_kind} budget exceeded" in errors[0]
    assert len(gateway.roles) == 3
    assert sorted(attempts.values()) == [1, 2]
    assert ledger.usage()["calls_used"] == 3
    assert ledger.usage()["tokens_reserved"] == 0
    assert ledger.usage()["tokens_used"] <= ledger.limits.max_tokens


def test_parallel_runs_keep_independent_call_budgets_on_one_orchestrator(
    session_factory, registry, provider
) -> None:
    barrier = Barrier(2, timeout=5)

    class OverlappingRunsGateway(FakeGateway):
        def complete(self, role, messages):
            response = super().complete(role, messages)
            if role == "researcher":
                barrier.wait()
            return response

    config = _config()
    config = config.model_copy(
        update={"research_budget": config.research_budget.model_copy(update={"max_llm_calls_per_run": 4})}
    )
    gateway = OverlappingRunsGateway()
    orchestrator = AgentResearchOrchestrator(
        config=config,
        gateway=gateway,
        experiments=ExperimentService(session_factory),
        factor_lab=FactorLab(session_factory, provider),
        registry=registry,
    )
    request = AgentResearchRequest(
        topic="Independent run budgets", start_date="2024-02-01", end_date="2024-05-31"
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(orchestrator.run, request) for _ in range(2)]
        results = [future.result() for future in futures]

    assert len({result.trace_id for result in results}) == 2
    assert len(gateway.roles) == 8
    for result in results:
        assert [call.role for call in result.calls] == [
            "researcher",
            "critic",
            "statistician",
            "orchestrator",
        ]
        assert result.experiment.state == ExperimentState.CANDIDATE


@pytest.mark.parametrize("rejecting_gate", ["statistician", "orchestrator", "local"])
def test_candidate_acceptance_still_requires_every_gate(
    session_factory, registry, provider, rejecting_gate
) -> None:
    config = _config()
    gateway = FakeGateway()
    if rejecting_gate == "statistician":
        gateway.responses["statistician"]["robust"] = False
    elif rejecting_gate == "orchestrator":
        gateway.responses["orchestrator"]["recommend_candidate"] = False
    else:
        config = config.model_copy(
            update={"factor_validation": config.factor_validation.model_copy(update={"min_ic_days": 10000})}
        )
    orchestrator = AgentResearchOrchestrator(
        config=config,
        gateway=gateway,
        experiments=ExperimentService(session_factory),
        factor_lab=FactorLab(session_factory, provider),
        registry=registry,
    )

    result = orchestrator.run(
        AgentResearchRequest(
            topic="All acceptance gates remain mandatory", start_date="2024-02-01", end_date="2024-05-31"
        )
    )

    assert result.experiment.state == ExperimentState.REJECTED
    assert result.factor_run.status == "COMPLETED"
    assert gateway.roles.count("researcher") == 1
    assert len(result.calls) == 4


@pytest.mark.parametrize("binding", ["unresolved", "different_path", "tampered"])
def test_snapshot_binding_fails_before_any_provider_request(
    session_factory, registry, store, provider, binding
):
    snapshots = DataSnapshotService(store, registry)
    snapshot = snapshots.create_snapshot(str(provider.data_dir), producer="factor-test")
    frozen = snapshots.resolve_path(snapshot.artifact_id)
    if binding == "tampered":
        (frozen / "securities.json").write_text("[]")
    gateway = FakeGateway()
    orchestrator = AgentResearchOrchestrator(
        config=_config(),
        gateway=gateway,
        experiments=ExperimentService(session_factory),
        factor_lab=FactorLab(session_factory, provider),
        registry=registry,
        store=store if binding != "unresolved" else None,
    )
    with pytest.raises(
        QuantSystemError,
        match={
            "unresolved": "snapshot resolver",
            "different_path": "not bound",
            "tampered": "integrity check failed",
        }[binding],
    ):
        orchestrator.run(
            AgentResearchRequest(
                topic="Verify actual factor snapshot",
                start_date="2024-02-01",
                end_date="2024-05-31",
                data_snapshot_id=snapshot.artifact_id,
            )
        )
    assert gateway.roles == []


def test_snapshot_binding_preserves_development_provider_and_date_cap(
    session_factory, registry, store, provider, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    development_provider = importlib.import_module("run_agent_backtest").DevelopmentProvider
    snapshots = DataSnapshotService(store, registry)
    snapshot = snapshots.create_snapshot(str(provider.data_dir), producer="factor-test")
    frozen = snapshots.resolve_path(snapshot.artifact_id)
    end = date(2024, 3, 29)
    reads = []

    class ObservedDevelopmentProvider(development_provider):
        def get_bars(self, symbols, start, end, *, as_of):
            bars = super().get_bars(symbols, start, end, as_of=as_of)
            reads.extend(bar.trade_date for bar in bars)
            assert all(bar.trade_date <= self.end for bar in bars)
            return bars

    bound_provider = ObservedDevelopmentProvider(str(frozen), end)
    lab = FactorLab(session_factory, bound_provider)

    def unexpected_factory(provider):
        pytest.fail("an already snapshot-bound development provider must not be replaced")

    gateway = FakeGateway()
    orchestrator = AgentResearchOrchestrator(
        config=_config(),
        gateway=gateway,
        experiments=ExperimentService(session_factory),
        factor_lab=lab,
        registry=registry,
        store=store,
        snapshot_service=snapshots,
        factor_lab_factory=unexpected_factory,
    )
    result = orchestrator.run(
        AgentResearchRequest(
            topic="Preserve development cutoff",
            start_date="2024-02-01",
            end_date="2024-05-31",
            data_snapshot_id=snapshot.artifact_id,
        )
    )

    assert reads and max(reads) <= end
    assert lab._provider is bound_provider
    assert all(point["date"] <= end.isoformat() for point in result.factor_run.ic_series)
    assert result.experiment_run.data_snapshot_id == snapshot.artifact_id
    report = registry.get(result.report_artifact_id)
    assert report.data_snapshot_id == snapshot.artifact_id
    assert len(report.metadata["call_receipts"]) == 4
    for reference in report.metadata["call_receipts"]:
        receipt = store.get_json(reference["uri"])
        assert receipt["status"] == "COMPLETED"
        assert receipt["messages"] and receipt["response"]["raw_request_id"]


def test_standalone_failure_persists_request_before_send_and_failed_receipt(
    session_factory, registry, store, provider
):
    class FailingGateway(FakeGateway):
        def complete(self, role, messages):
            paths = list((store.root / "research/factor/calls").rglob("*.json"))
            assert len(paths) == 1
            receipt = store.get_json(paths[0].as_uri())
            assert receipt["status"] == "STARTED"
            assert receipt["messages"] == [message.model_dump(mode="json") for message in messages]
            self.roles.append(role)
            raise QuantSystemError("provider unavailable")

    experiments = ExperimentService(session_factory)
    gateway = FailingGateway()
    orchestrator = AgentResearchOrchestrator(
        config=_config(),
        gateway=gateway,
        experiments=experiments,
        factor_lab=FactorLab(session_factory, provider),
        registry=registry,
        store=store,
    )
    with pytest.raises(QuantSystemError, match="provider unavailable"):
        orchestrator.run(
            AgentResearchRequest(
                topic="Retain failed model call", start_date="2024-02-01", end_date="2024-05-31"
            )
        )

    assert gateway.roles == ["researcher"]
    receipts = [
        store.get_json(path.as_uri()) for path in (store.root / "research/factor/calls").rglob("*.json")
    ]
    assert {receipt["status"] for receipt in receipts} == {"STARTED", "FAILED"}
    failed = next(receipt for receipt in receipts if receipt["status"] == "FAILED")
    assert failed["messages"]
    assert failed["response"] is None
    assert failed["error"] == "provider unavailable"
    assert failed["used_tokens"] == failed["reservation_tokens"] > 0
    assert registry.find_by_type(ArtifactType.RESEARCH_REPORT) == []


def test_audit_store_failure_prevents_paid_call_and_keeps_domain_error(
    session_factory, registry, store, provider, monkeypatch
):
    def unavailable_store(*args, **kwargs):
        raise OSError("audit disk unavailable")

    monkeypatch.setattr(store, "put_content_addressed_json", unavailable_store)
    gateway = FakeGateway()
    orchestrator = AgentResearchOrchestrator(
        config=_config(),
        gateway=gateway,
        experiments=ExperimentService(session_factory),
        factor_lab=FactorLab(session_factory, provider),
        registry=registry,
        store=store,
    )
    with pytest.raises(QuantSystemError, match="call audit sink failed"):
        orchestrator.run(
            AgentResearchRequest(
                topic="Fail closed on unavailable audit", start_date="2024-02-01", end_date="2024-05-31"
            )
        )
    assert gateway.roles == []
