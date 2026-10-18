from __future__ import annotations

import importlib
import json
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from quant_system.agents.config import AgentLLMConfig
from quant_system.agents.llm import LLMResponse
from quant_system.agents.orchestration import AgentResearchOrchestrator, AgentResearchResult
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.data.snapshots.service import DataSnapshotService
from quant_system.domain.enums import ArtifactType, ExperimentState
from quant_system.factors.evaluation import FactorLab
from quant_system.fixtures import generate_fixture_data
from quant_system.research.experiments import ExperimentService
from quant_system.research.modules.factor import FactorTaskRequest, factor_tasks
from quant_system.research.runtime.calls import BudgetLimits
from quant_system.research.runtime.engine import ResearchRuntime
from quant_system.research.runtime.models import PlanTask, ResearchPlan, RunRequest
from quant_system.research.runtime.ports import ModuleRegistry
from quant_system.research.runtime.storage import ArtifactRunRepository


@pytest.fixture
def adjusted_provider(tmp_path):
    source = LocalParquetProvider(generate_fixture_data(str(tmp_path / "adjusted-data"), seed=42))
    adjustments = pa.Table.from_pylist(
        [
            {
                "symbol": bar.symbol,
                "trade_date": bar.trade_date,
                "factor": 1 + (bar.trade_date - date(2024, 1, 1)).days / 1000,
                "available_at": bar.available_at,
            }
            for bar in source._load_bars()
        ]
    )
    pq.write_table(adjustments, source.data_dir / "adjustments.parquet")
    return source


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
            "factor_validation": {"min_ic_days": 1, "min_coverage": 0, "min_abs_rank_ic": 0},
        }
    )


def _payload(**changes):
    return {
        "topic": "Factor task compatibility",
        "start_date": "2024-02-01",
        "end_date": "2024-05-31",
        **changes,
    }


class FactorGateway:
    def __init__(self, *, rejected=False, malformed_statistician=False):
        self.roles = []
        self.malformed_statistician = malformed_statistician
        self.responses = {
            "researcher": {
                "name": "Momentum",
                "hypothesis": "Trend persistence",
                "expression": "rank(delta(close,20))",
                "horizon": 5,
            },
            "critic": {"approved": not rejected, "issues": [], "rationale": "Local evidence"},
            "statistician": {"robust": True, "findings": [], "rationale": "Local statistics"},
            "orchestrator": {
                "recommend_candidate": True,
                "rationale": "Keep local gates",
                "next_steps": ["Independent validation"],
            },
        }

    def complete(self, role, messages):
        self.roles.append(role)
        content = json.dumps(self.responses[role])
        if role == "statistician" and self.malformed_statistician:
            self.malformed_statistician = False
            content = "invalid JSON"
        return LLMResponse(
            content=content,
            provider="fake",
            model=f"fake-{role}",
            input_tokens=10,
            output_tokens=5,
            raw_request_id=f"request-{role}",
        )


def _runtime(
    session_factory,
    registry,
    store,
    provider,
    gateway,
    *,
    config=None,
    factory=False,
    factor_lab_factory=None,
    adjusted_features=False,
):
    config = config or _config()
    service = AgentResearchOrchestrator(
        config=config,
        gateway=gateway,
        experiments=ExperimentService(session_factory),
        factor_lab=FactorLab(session_factory, provider, adjusted_features=adjusted_features),
        registry=registry,
        store=store,
        factor_lab_factory=factor_lab_factory,
    )
    requests = []

    def service_factory(request):
        requests.append(request)
        return service

    modules = ModuleRegistry()
    for task in factor_tasks(service_factory if factory else service):
        modules.register(task)
    repository = ArtifactRunRepository(store, registry)
    runtime = ResearchRuntime(modules, repository, config, gateway, credentials_required=False)
    return runtime, requests


@pytest.mark.parametrize(
    "changes",
    [
        {"data_mode": "frozen_snapshot"},
        {"data_mode": "live"},
        {"unknown": True},
        {"start_date": "2024-05-31"},
        {"data_snapshot_id": "snapshot_without_mode"},
    ],
)
def test_factor_task_rejects_invalid_or_ambiguous_requests(changes):
    with pytest.raises(ValidationError):
        FactorTaskRequest.model_validate(_payload(**changes))


def test_factor_task_descriptor_keeps_four_roles_and_strict_schema():
    (task,) = factor_tasks(lambda request: None)
    descriptor = task.describe()
    assert descriptor.module_id == "factor"
    assert descriptor.task_type == "factor.research"
    assert descriptor.default_budget.max_calls == 30
    assert descriptor.roles == {
        role: role for role in ("researcher", "critic", "statistician", "orchestrator")
    }
    assert descriptor.request_schema["additionalProperties"] is False
    assert descriptor.request_schema["properties"]["data_mode"]["default"] == "fixture"
    assert FactorTaskRequest.model_validate(_payload()).data_mode == "fixture"


@pytest.mark.parametrize("rejected", [False, True])
def test_factor_task_uses_runtime_once_and_preserves_legacy_result(
    session_factory, registry, store, provider, rejected
):
    gateway = FactorGateway(rejected=rejected)
    runtime, requests = _runtime(session_factory, registry, store, provider, gateway, factory=True)
    submitted = runtime.submit(RunRequest(task_type="factor.research", payload=_payload()))
    runtime.execute(submitted.run_id)
    run = runtime.get(submitted.run_id)

    assert run.status == "COMPLETED"
    assert run.result is not None
    assert len(requests) == 1
    assert isinstance(requests[0], FactorTaskRequest)
    result = AgentResearchResult.model_validate(run.result.payload)
    expected = ExperimentState.REJECTED if rejected else ExperimentState.CANDIDATE
    assert result.experiment.state == expected
    assert run.result.outcome == expected.value
    assert run.result.summary == result.decision.rationale
    assert run.result.publication.decision == "NOT_APPLICABLE"
    assert run.result.report_artifact_id == result.report_artifact_id
    assert len(gateway.roles) == run.usage["calls_used"] == len(result.calls) == 4
    assert run.usage["tokens_used"] == 60
    assert run.usage["tokens_reserved"] == 0
    assert [call.role for call in result.calls] == ["researcher", "critic", "statistician", "orchestrator"]
    report = registry.get(result.report_artifact_id)
    assert report.artifact_type == ArtifactType.RESEARCH_REPORT
    assert report.metadata["module_id"] == "factor"
    assert report.metadata["task_type"] == "factor.research"
    assert store.get_json(report.uri) == report.metadata
    receipts = [
        store.get_json(artifact.uri) for artifact in registry.find_by_type(ArtifactType.RESEARCH_CALL)
    ]
    terminal = {
        item["receipt"]["call_id"]: item["receipt"]
        for item in receipts
        if item["receipt"]["status"] == "COMPLETED"
    }
    assert len(terminal) == 4
    for reference in report.metadata["call_receipts"]:
        receipt = terminal[reference["call_id"]]
        assert reference["run_id"] == run.run_id
        assert receipt["messages"]
        assert receipt["response"]["raw_request_id"].startswith("request-")
    stages = [store.get_json(artifact.uri) for artifact in registry.find_by_type(ArtifactType.RESEARCH_EVENT)]
    stages = [
        item["stage"]
        for item in sorted(stages, key=lambda item: item["sequence"])
        if item["event_type"] == "stage"
    ]
    assert stages == ["executing", "candidate", "evaluation", "independent_review", "orchestrator", "report"]


@pytest.mark.parametrize("budget", [3, 4])
def test_factor_task_cannot_reset_runtime_call_budget(session_factory, registry, store, provider, budget):
    gateway = FactorGateway(malformed_statistician=True)
    runtime, _ = _runtime(session_factory, registry, store, provider, gateway)
    submitted = runtime.submit(
        RunRequest(task_type="factor.research", payload=_payload(), budget=BudgetLimits(max_calls=budget))
    )
    runtime.execute(submitted.run_id)
    run = runtime.get(submitted.run_id)
    assert run.status == "FAILED"
    assert "call budget exceeded" in run.error
    assert len(gateway.roles) == run.usage["calls_used"] == (0 if budget == 3 else 4)
    assert run.usage["tokens_reserved"] == 0
    assert "orchestrator" not in gateway.roles


def test_frozen_factor_task_consumes_snapshot_and_registers_actual_evidence(
    session_factory, registry, store, provider, monkeypatch
):
    snapshots = DataSnapshotService(store, registry)
    snapshot = snapshots.create_snapshot(str(provider.data_dir), producer="factor-test")
    frozen = snapshots.resolve_path(snapshot.artifact_id)
    reads = []
    get_bars = LocalParquetProvider.get_bars

    def observed_bars(visible_provider, *args, **kwargs):
        reads.append(visible_provider.data_dir.resolve())
        return get_bars(visible_provider, *args, **kwargs)

    monkeypatch.setattr(LocalParquetProvider, "get_bars", observed_bars)
    bound_providers = []

    def factor_lab_factory(bound_provider):
        bound_providers.append(bound_provider)
        return FactorLab(session_factory, bound_provider)

    gateway = FactorGateway()
    runtime, _ = _runtime(
        session_factory, registry, store, provider, gateway, factor_lab_factory=factor_lab_factory
    )
    submitted = runtime.submit(
        RunRequest(
            task_type="factor.research",
            payload=_payload(data_mode="frozen_snapshot", data_snapshot_id=snapshot.artifact_id),
        )
    )
    runtime.execute(submitted.run_id)
    run = runtime.get(submitted.run_id)

    assert run.status == "COMPLETED", run.error
    assert reads and set(reads) == {frozen.resolve()}
    assert len(bound_providers) == 1
    assert bound_providers[0]._bars
    assert run.result.evidence[0].artifact_id == snapshot.artifact_id
    assert run.result.evidence[0].content_hash == snapshot.content_hash
    assert run.usage["evidence_manifest"]["references"] == [run.result.evidence[0].model_dump(mode="json")]
    result = AgentResearchResult.model_validate(run.result.payload)
    assert result.factor_run.metrics["n_ic_days"] > 0
    assert result.experiment_run.data_snapshot_id == snapshot.artifact_id
    assert result.experiment_run.input_artifact_ids == [snapshot.artifact_id]
    report = registry.get(result.report_artifact_id)
    assert report.data_snapshot_id == snapshot.artifact_id
    assert snapshot.artifact_id in report.parent_artifact_ids


def test_factor_plan_children_share_root_budget_without_standalone_fallback(
    session_factory, registry, store, provider
):
    gateway = FactorGateway()
    runtime, _ = _runtime(session_factory, registry, store, provider, gateway)
    submitted = runtime.submit(
        RunRequest(
            plan=ResearchPlan(
                tasks=[
                    PlanTask(
                        task_id="first",
                        task_type="factor.research",
                        payload=_payload(),
                        budget=BudgetLimits(max_calls=4),
                    ),
                    PlanTask(
                        task_id="second",
                        task_type="factor.research",
                        payload=_payload(),
                        depends_on=["first"],
                        budget=BudgetLimits(max_calls=4),
                    ),
                ],
                budget=BudgetLimits(max_calls=7),
            ),
            budget=BudgetLimits(max_calls=7),
        )
    )
    runtime.execute(submitted.run_id)
    root = runtime.get(submitted.run_id)
    children = [runtime.get(child_id) for child_id in root.children]

    assert root.status == "FAILED"
    assert [child.status for child in children] == ["COMPLETED", "FAILED"]
    assert children[0].result.publication.decision == "NOT_APPLICABLE"
    assert "call budget exceeded" in children[1].error
    assert len(gateway.roles) == root.usage["calls_used"] == 4
    assert [child.usage["calls_used"] for child in children] == [4, 0]
    assert all(child.usage["root_usage"]["calls_used"] == 4 for child in children)
    assert root.usage["tokens_reserved"] == 0


def test_factor_schema_repairs_are_audited_and_charged_once(session_factory, registry, store, provider):
    gateway = FactorGateway(malformed_statistician=True)
    runtime, _ = _runtime(session_factory, registry, store, provider, gateway)
    submitted = runtime.submit(RunRequest(task_type="factor.research", payload=_payload()))
    runtime.execute(submitted.run_id)
    run = runtime.get(submitted.run_id)
    assert run.status == "COMPLETED", run.error
    assert len(gateway.roles) == run.usage["calls_used"] == len(run.result.payload["calls"]) == 5
    assert run.usage["tokens_used"] == 75
    receipts = runtime.calls(run.run_id)
    invalid = next(receipt for receipt in receipts if receipt.status == "INVALID")
    repaired = next(receipt for receipt in receipts if receipt.repair_of == invalid.call_id)
    assert invalid.role == repaired.role == "statistician"
    assert invalid.response.content == "invalid JSON"
    assert repaired.status == "COMPLETED"
    assert repaired.messages[-2].role == "assistant"
    assert repaired.messages[-2].content == invalid.response.content


def test_snapshot_factory_cannot_claim_snapshot_for_a_different_provider(
    session_factory, registry, store, provider
):
    snapshots = DataSnapshotService(store, registry)
    snapshot = snapshots.create_snapshot(str(provider.data_dir), producer="factor-test")
    gateway = FactorGateway()
    runtime, _ = _runtime(
        session_factory,
        registry,
        store,
        provider,
        gateway,
        factor_lab_factory=lambda bound_provider: FactorLab(session_factory, provider),
    )
    submitted = runtime.submit(
        RunRequest(
            task_type="factor.research",
            payload=_payload(data_mode="frozen_snapshot", data_snapshot_id=snapshot.artifact_id),
        )
    )
    runtime.execute(submitted.run_id)
    run = runtime.get(submitted.run_id)
    assert run.status == "FAILED"
    assert "not bound" in run.error
    assert gateway.roles == []
    assert run.usage["calls_used"] == 0
    assert run.result is None


@pytest.mark.parametrize("binding_mode", ["injected", "factory", "factory_mutates_input"])
def test_snapshot_binding_rejects_retargeted_cached_provider_before_model_calls(
    session_factory, registry, store, provider, tmp_path, binding_mode
):
    snapshots = DataSnapshotService(store, registry)
    snapshot = snapshots.create_snapshot(str(provider.data_dir), producer="factor-test")
    frozen = snapshots.resolve_path(snapshot.artifact_id)
    cached_provider = LocalParquetProvider(generate_fixture_data(str(tmp_path / "other-data"), seed=43))
    cached_bars = cached_provider._load_bars()
    assert cached_bars != LocalParquetProvider(str(frozen))._load_bars()
    cached_provider.data_dir = frozen

    def factor_lab_factory(bound_provider):
        if binding_mode == "factory_mutates_input":
            bound_provider._bars = cached_bars
            return FactorLab(session_factory, bound_provider)
        return FactorLab(session_factory, cached_provider)

    gateway = FactorGateway()
    runtime, _ = _runtime(
        session_factory,
        registry,
        store,
        cached_provider if binding_mode == "injected" else provider,
        gateway,
        factor_lab_factory=None if binding_mode == "injected" else factor_lab_factory,
    )
    submitted = runtime.submit(
        RunRequest(
            task_type="factor.research",
            payload=_payload(data_mode="frozen_snapshot", data_snapshot_id=snapshot.artifact_id),
        )
    )
    runtime.execute(submitted.run_id)
    run = runtime.get(submitted.run_id)

    assert run.status == "FAILED"
    assert "not bound" in run.error
    assert gateway.roles == []
    assert run.usage["calls_used"] == 0
    assert run.result is None
    assert snapshots.resolve_path(snapshot.artifact_id) == frozen


def test_snapshot_binding_uses_a_fresh_provider_without_mutating_the_injected_lab(
    session_factory, registry, store, provider, monkeypatch
):
    snapshots = DataSnapshotService(store, registry)
    snapshot = snapshots.create_snapshot(str(provider.data_dir), producer="factor-test")
    frozen = snapshots.resolve_path(snapshot.artifact_id)
    injected_provider = LocalParquetProvider(str(frozen))
    injected_lab = FactorLab(session_factory, injected_provider)
    consumed_providers = []
    evaluate = FactorLab.evaluate

    def observed_evaluate(lab, **kwargs):
        consumed_providers.append(lab._provider)
        return evaluate(lab, **kwargs)

    monkeypatch.setattr(FactorLab, "evaluate", observed_evaluate)
    gateway = FactorGateway()
    runtime, _ = _runtime(
        session_factory,
        registry,
        store,
        provider,
        gateway,
        factor_lab_factory=lambda bound_provider: injected_lab,
    )
    submitted = runtime.submit(
        RunRequest(
            task_type="factor.research",
            payload=_payload(data_mode="frozen_snapshot", data_snapshot_id=snapshot.artifact_id),
        )
    )
    runtime.execute(submitted.run_id)
    run = runtime.get(submitted.run_id)

    assert run.status == "COMPLETED", run.error
    assert len(consumed_providers) == 1
    assert type(consumed_providers[0]) is LocalParquetProvider
    assert consumed_providers[0] is not injected_provider
    assert consumed_providers[0].data_dir == frozen.resolve()
    assert injected_lab._provider is injected_provider


@pytest.mark.parametrize("through_factory", [False, True])
def test_snapshot_binding_preserves_development_cutoff_and_adjusted_features(
    session_factory, registry, store, adjusted_provider, monkeypatch, through_factory
):
    cutoff = date(2024, 3, 29)
    observed_days: list[date] = []
    adjusted_reads = []
    adjusted_closes = LocalParquetProvider.adjusted_closes
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    development_provider_class = importlib.import_module("run_agent_backtest").DevelopmentProvider
    development_bars = development_provider_class.get_bars

    def observed_development_bars(source, symbols, start, end, *, as_of):
        bars = development_bars(source, symbols, start, end, as_of=as_of)
        observed_days.extend(bar.trade_date for bar in bars)
        return bars

    def observed_adjusted_closes(source, bars, *, as_of):
        adjusted_reads.append(source.data_dir.resolve())
        return adjusted_closes(source, bars, as_of=as_of)

    monkeypatch.setattr(development_provider_class, "get_bars", observed_development_bars)
    monkeypatch.setattr(LocalParquetProvider, "adjusted_closes", observed_adjusted_closes)
    snapshots = DataSnapshotService(store, registry)
    snapshot = snapshots.create_snapshot(str(adjusted_provider.data_dir), producer="factor-test")
    frozen = snapshots.resolve_path(snapshot.artifact_id)
    expected = FactorLab(session_factory, LocalParquetProvider(str(frozen)), adjusted_features=True).evaluate(
        expression="rank(delta(close,20))",
        universe_id="cn_a_all",
        start_date=date(2024, 2, 1),
        end_date=cutoff,
        target="NEXT_OPEN_TO_CLOSE",
        horizon=5,
    )
    adjusted_reads.clear()

    def factor_lab_factory(bound_provider):
        return FactorLab(
            session_factory,
            development_provider_class(str(bound_provider.data_dir), cutoff),
            adjusted_features=True,
        )

    gateway = FactorGateway()
    runtime, _ = _runtime(
        session_factory,
        registry,
        store,
        adjusted_provider if through_factory else development_provider_class(str(frozen), cutoff),
        gateway,
        factor_lab_factory=factor_lab_factory if through_factory else None,
        adjusted_features=True,
    )
    submitted = runtime.submit(
        RunRequest(
            task_type="factor.research",
            payload=_payload(data_mode="frozen_snapshot", data_snapshot_id=snapshot.artifact_id),
        )
    )
    runtime.execute(submitted.run_id)
    run = runtime.get(submitted.run_id)

    assert run.status == "COMPLETED", run.error
    result = AgentResearchResult.model_validate(run.result.payload)
    assert result.factor_run.metrics == expected.metrics
    assert result.factor_run.ic_series == expected.ic_series
    assert observed_days and max(observed_days) == cutoff
    assert adjusted_reads and set(adjusted_reads) == {frozen.resolve()}


@pytest.mark.parametrize("fabrication", ["bars", "calendar", "adjustments", "later_bars"])
def test_snapshot_binding_validates_subclass_outputs_instead_of_trusting_its_path(
    session_factory, registry, store, adjusted_provider, fabrication
):
    class FabricatedProvider(LocalParquetProvider):
        def get_bars(self, symbols, start, end, *, as_of):
            bars = super().get_bars(symbols, start, end, as_of=as_of)
            if bars and (fabrication == "bars" or (fabrication == "later_bars" and end < date(2024, 5, 31))):
                bars[0] = bars[0].model_copy(update={"volume": bars[0].volume + 1})
            return bars

        def trading_calendar(self, start, end):
            days = super().trading_calendar(start, end)
            return [*days, end + timedelta(days=1)] if fabrication == "calendar" else days

        def adjusted_closes(self, bars, *, as_of):
            adjusted = super().adjusted_closes(bars, as_of=as_of)
            if fabrication == "adjustments":
                return {key: value * 2 for key, value in adjusted.items()}
            return adjusted

    snapshots = DataSnapshotService(store, registry)
    snapshot = snapshots.create_snapshot(str(adjusted_provider.data_dir), producer="factor-test")
    gateway = FactorGateway()
    runtime, _ = _runtime(
        session_factory,
        registry,
        store,
        adjusted_provider,
        gateway,
        factor_lab_factory=lambda bound_provider: FactorLab(
            session_factory, FabricatedProvider(str(bound_provider.data_dir)), adjusted_features=True
        ),
    )
    submitted = runtime.submit(
        RunRequest(
            task_type="factor.research",
            payload=_payload(data_mode="frozen_snapshot", data_snapshot_id=snapshot.artifact_id),
        )
    )
    runtime.execute(submitted.run_id)
    run = runtime.get(submitted.run_id)

    assert run.status == "FAILED"
    assert "not bound" in run.error
    assert gateway.roles == (["researcher"] if fabrication == "later_bars" else [])
    assert run.usage["calls_used"] == len(gateway.roles)
    assert run.result is None


@pytest.mark.parametrize("changed_during", ["candidate", "evaluation"])
def test_snapshot_binding_rechecks_integrity_immediately_before_and_after_evaluation(
    session_factory, registry, store, provider, monkeypatch, changed_during
):
    snapshots = DataSnapshotService(store, registry)
    snapshot = snapshots.create_snapshot(str(provider.data_dir), producer="factor-test")
    frozen = snapshots.resolve_path(snapshot.artifact_id)
    gateway = FactorGateway()
    complete = gateway.complete
    evaluate = FactorLab.evaluate
    evaluations = []

    def change_snapshot():
        metadata_path = frozen / "securities.json"
        metadata_path.write_bytes(metadata_path.read_bytes() + b"\n")

    def observed_complete(role, messages):
        response = complete(role, messages)
        if role == "researcher" and changed_during == "candidate":
            change_snapshot()
        return response

    def observed_evaluate(lab, **kwargs):
        evaluations.append(lab._provider)
        result = evaluate(lab, **kwargs)
        if changed_during == "evaluation":
            change_snapshot()
        return result

    monkeypatch.setattr(gateway, "complete", observed_complete)
    monkeypatch.setattr(FactorLab, "evaluate", observed_evaluate)
    runtime, _ = _runtime(
        session_factory,
        registry,
        store,
        provider,
        gateway,
        factor_lab_factory=lambda bound_provider: FactorLab(session_factory, bound_provider),
    )
    submitted = runtime.submit(
        RunRequest(
            task_type="factor.research",
            payload=_payload(data_mode="frozen_snapshot", data_snapshot_id=snapshot.artifact_id),
        )
    )
    runtime.execute(submitted.run_id)
    run = runtime.get(submitted.run_id)

    assert run.status == "FAILED"
    assert "integrity check failed" in run.error
    assert len(evaluations) == (1 if changed_during == "evaluation" else 0)
    assert gateway.roles == ["researcher"]
    assert run.usage["calls_used"] == 1
    assert run.result is None


def test_snapshot_unavailable_at_historical_cutoff_is_rejected_before_model_calls(
    session_factory, registry, store, provider
):
    snapshots = DataSnapshotService(store, registry)
    snapshot = snapshots.create_snapshot(str(provider.data_dir), producer="factor-test")
    gateway = FactorGateway()
    runtime, _ = _runtime(
        session_factory,
        registry,
        store,
        provider,
        gateway,
        factor_lab_factory=lambda bound_provider: FactorLab(session_factory, bound_provider),
    )
    submitted = runtime.submit(
        RunRequest(
            task_type="factor.research",
            payload=_payload(
                data_mode="frozen_snapshot",
                data_snapshot_id=snapshot.artifact_id,
            ),
            mode="HISTORICAL",
            knowledge_cutoff=snapshot.created_at - timedelta(seconds=1),
        )
    )
    runtime.execute(submitted.run_id)
    run = runtime.get(submitted.run_id)
    assert run.status == "FAILED"
    assert "unavailable at the knowledge cutoff" in run.error
    assert gateway.roles == []
    assert run.usage["calls_used"] == 0
    assert run.result is None
