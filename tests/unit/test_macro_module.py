from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Barrier, Lock
from typing import Any, cast

import pytest

from quant_system.agents.config import AgentLLMConfig
from quant_system.agents.deliberation import EvidenceItem
from quant_system.agents.llm import LLMResponse
from quant_system.domain.enums import ArtifactType
from quant_system.domain.exceptions import QuantSystemError
from quant_system.macro.ingestion import FRED_OBSERVATIONS_URL
from quant_system.macro.models import MacroCollectRequest
from quant_system.macro.service import MacroService
from quant_system.research.modules.macro import (
    MacroResearchRequest,
    macro_gold_facts,
    macro_tasks,
    macro_tools,
)
from quant_system.research.runtime.calls import BudgetLimits
from quant_system.research.runtime.engine import ResearchRuntime, RunContext
from quant_system.research.runtime.evidence import validate_evidence
from quant_system.research.runtime.models import (
    EvidenceRef,
    InputRef,
    PlanTask,
    Publication,
    ResearchPlan,
    RunRequest,
    TaskResult,
)
from quant_system.research.runtime.ports import ModuleRegistry
from quant_system.research.runtime.storage import ArtifactRunRepository
from quant_system.research.runtime.tools import ToolExecutor

FETCHED_AT = datetime(2026, 10, 17, 8, tzinfo=UTC)


class OfflineTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, *, params: Mapping[str, str], timeout: float) -> bytes:
        assert url == FRED_OBSERVATIONS_URL
        assert timeout > 0
        self.calls.append(params["series_id"])
        return json.dumps(
            {
                "count": 2,
                "offset": 0,
                "observations": [
                    {"date": "2026-10-15", "value": "4.0"},
                    {"date": "2026-10-16", "value": "4.1"},
                ],
            }
        ).encode()


class NoModelGateway:
    def complete(self, *_args):
        raise AssertionError("macro data tasks must not call a model")


def _runtime(store, registry, service, *, config=None, gateway=None):
    config = config or AgentLLMConfig(providers={}, roles={})
    modules = ModuleRegistry()
    for task in macro_tasks(service, config, store=store, registry=registry):
        modules.register(task)

    def clock():
        return FETCHED_AT + timedelta(seconds=1)

    runtime = ResearchRuntime(
        modules,
        ArtifactRunRepository(store, registry, clock=clock),
        config,
        gateway or NoModelGateway(),
        clock=clock,
        credentials_required=False,
    )
    for tool in macro_tools(service):
        runtime.tools.register(tool)
    return runtime


def test_macro_collect_uses_registered_typed_tool_without_llm(store, registry, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    transport = OfflineTransport()
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=transport)
    runtime = _runtime(store, registry, service)
    descriptor = runtime.module_registry.get("macro.collect").describe()
    assert descriptor.acquisition
    assert descriptor.roles == {}
    assert descriptor.tools == {"system": ["macro.sources.collect"]}
    assert descriptor.capabilities == ["macro.network.read"]
    run = runtime.submit(
        RunRequest(
            task_type="macro.collect",
            payload={"series_ids": ["DGS10"], "start_date": "2026-10-01"},
        )
    )
    runtime.execute(run.run_id)
    completed = runtime.get(run.run_id)
    assert completed.status == "COMPLETED", completed.error
    assert completed.result is not None
    snapshot_id = completed.result.payload["snapshot_id"]
    assert isinstance(snapshot_id, str)
    artifact = registry.get(snapshot_id)
    reference = completed.result.evidence[0]
    assert reference.kind == "FACT"
    assert reference.namespace == "macro"
    assert reference.schema_version == artifact.schema_version == "macro-snapshot-v1"
    assert reference.content_hash == artifact.content_hash
    assert reference.available_at == FETCHED_AT
    assert completed.result.publication.decision == "NOT_APPLICABLE"
    assert transport.calls == ["DGS10"]
    assert runtime.calls(run.run_id) == []
    assert any(event.event_type == "tool_completed" for event in runtime.events(run.run_id))


def test_macro_collect_then_analyze_plan_uses_facts_and_shared_cutoff(store, registry, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    transport = OfflineTransport()
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=transport)
    runtime = _runtime(store, registry, service)
    descriptor = runtime.module_registry.get("macro.analyze").describe()
    assert descriptor.roles == {}
    assert descriptor.result_schema_version == "macro-analysis-v1"
    assert descriptor.tools == {"system": ["macro.snapshot.read"]}
    run = runtime.submit(
        RunRequest(
            plan=ResearchPlan(
                tasks=[
                    PlanTask(
                        task_id="collect",
                        task_type="macro.collect",
                        payload={"series_ids": ["DGS10"], "start_date": "2026-10-01"},
                    ),
                    PlanTask(
                        task_id="analyze",
                        task_type="macro.analyze",
                        depends_on=["collect"],
                        inputs={"snapshot_id": InputRef(task_id="collect", output_key="snapshot_id")},
                    ),
                ]
            )
        )
    )
    runtime.execute(run.run_id)
    completed = runtime.get(run.run_id)
    assert completed.status == "COMPLETED", completed.error
    collect, analyze = [runtime.get(child_id) for child_id in completed.children]
    assert collect.result is not None and analyze.result is not None
    assert collect.result.payload["snapshot_id"] == analyze.result.payload["snapshot_id"]
    facts = analyze.result.payload
    assert facts["indicators"]["DGS10"]["latest_value"] == 4.1
    assert facts["knowledge_cutoff"] == completed.knowledge_cutoff.isoformat()
    assert facts["missing"] == {}
    assert set(facts["coverage"]) == {"DGS10"}
    assert "perspectives" not in facts
    assert transport.calls == ["DGS10"]
    assert completed.usage["calls_used"] == 0
    report = registry.get(analyze.result.report_artifact_id)
    assert report.artifact_type == ArtifactType.RESEARCH_REPORT
    assert report.schema_version == "macro-analysis-v1"
    assert report.metadata["module_id"] == "macro"
    assert report.metadata["task_type"] == "macro.analyze"
    assert report.metadata["evidence_kind"] == "OPINION"
    assert report.metadata["approved_publication_hash"] is None
    assert set(report.parent_artifact_ids) == {facts["snapshot_id"], facts["raw_snapshot_id"]}
    assert report.data_snapshot_id == facts["raw_snapshot_id"]
    assert store.get_json(report.uri)["no_investment_advice"] is True


def test_macro_analyze_before_availability_returns_no_references_or_model_calls(store, registry, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    transport = OfflineTransport()
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=transport)
    collection = service.collect(MacroCollectRequest(series_ids=["DGS10"]))
    runtime = _runtime(store, registry, service)
    run = runtime.submit(
        RunRequest(
            task_type="macro.analyze",
            payload={"snapshot_id": collection.snapshot_id},
            mode="HISTORICAL",
            knowledge_cutoff=FETCHED_AT - timedelta(seconds=1),
        )
    )
    runtime.execute(run.run_id)
    completed = runtime.get(run.run_id)
    assert completed.status == "COMPLETED", completed.error
    assert completed.result.outcome == "INSUFFICIENT_EVIDENCE"
    assert completed.result.evidence == []
    assert completed.result.payload["indicators"] == {}
    assert completed.result.payload["observations"] == []
    assert completed.result.payload["evidence"] == []
    assert runtime.calls(run.run_id) == []
    assert transport.calls == ["DGS10"]


def _config():
    return AgentLLMConfig.model_validate(
        {
            "providers": {
                "fake": {
                    "kind": "openai",
                    "base_url": "https://example.invalid",
                    "api_key_env": "UNUSED_MACRO_TEST_KEY",
                }
            },
            "roles": {
                role: {"provider": "fake", "model": "offline", "max_output_tokens": 500}
                for role in ("statistician", "critic")
            },
        }
    )


class ViewGateway:
    def __init__(self, *, parallel=False, defect=None, repair=False):
        self.messages = {}
        self.records = []
        self.lock = Lock()
        self.barrier = Barrier(2) if parallel else None
        self.active = 0
        self.peak = 0
        self.defect = defect
        self.repair = repair

    def complete(self, role, messages):
        with self.lock:
            self.messages[role] = messages
            self.records.append((role, messages))
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=10)
            user_message = next(message for message in messages if message.role == "user")
            facts = json.loads(user_message.content)["facts"]
            evidence_id = facts["indicators"]["DGS10"]["evidence_ids"][0]
            payload: dict[str, Any] = {
                "summary": f"Independent {role} evidence review.",
                "claims": [{"claim": f"{role} reviewed the observed yield.", "evidence_ids": [evidence_id]}],
                "missing": [],
                "risks": ["Historical availability is not PIT certified."],
                "state": "EVIDENCE_BOUNDED",
            }
            if self.defect and (not self.repair or (role == "macro.rates" and len(messages) == 2)):
                if self.defect == "unknown_id":
                    payload["claims"][0]["evidence_ids"] = ["macro:unavailable:DFII10:2026-10-16"]
                elif self.defect == "direction":
                    payload["state"] = "BULLISH"
                elif self.defect == "extra_url":
                    payload["url"] = "https://example.invalid/unregistered"
                elif self.defect == "missing_claims":
                    payload["claims"] = []
                elif self.defect == "duplicate_ids":
                    payload["claims"][0]["evidence_ids"] = [evidence_id, evidence_id]
                else:
                    raise AssertionError("unknown test defect")
            return LLMResponse(
                content=json.dumps(payload),
                provider="fake",
                model="offline",
                input_tokens=10,
                output_tokens=10,
            )
        finally:
            with self.lock:
                self.active -= 1


def test_macro_research_two_independent_views_no_third_call(store, registry, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    transport = OfflineTransport()
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=transport)
    collection = service.collect(MacroCollectRequest(series_ids=["DGS10"]))
    gateway = ViewGateway(parallel=True)
    runtime = _runtime(store, registry, service, config=_config(), gateway=gateway)
    descriptor = runtime.module_registry.get("macro.research").describe()
    assert descriptor.roles == {"macro.rates": "statistician", "macro.risks": "critic"}
    assert descriptor.default_budget.max_parallel_calls == 2
    run = runtime.submit(
        RunRequest(
            task_type="macro.research",
            payload={"snapshot_id": collection.snapshot_id, "focus": "Assess source limitations."},
        )
    )
    runtime.execute(run.run_id)
    completed = runtime.get(run.run_id)
    assert completed.status == "COMPLETED", completed.error
    result = completed.result
    assert result.outcome == "EVIDENCE_BOUNDED"
    assert result.publication.decision == "NOT_APPLICABLE"
    assert result.publication.content_hash is None
    assert result.payload["snapshot_id"] == collection.snapshot_id
    assert result.payload["synthesis"]["state"] == "EVIDENCE_BOUNDED"
    assert set(result.payload["perspectives"]) == {"macro.rates", "macro.risks"}
    assert result.payload["no_investment_advice"] is True
    assert completed.usage["calls_used"] == 2
    assert completed.usage["tokens_used"] == 40
    assert len(runtime.calls(run.run_id)) == 2
    assert gateway.peak == 2
    rates_messages = gateway.messages["macro.rates"]
    risks_messages = gateway.messages["macro.risks"]
    assert rates_messages[-1].content == risks_messages[-1].content
    rates_facts = json.loads(rates_messages[-1].content)["facts"]
    assert rates_facts["indicators"]["DGS10"]["latest_value"] == 4.1
    assert rates_facts["indicators"]["DGS10"]["change_from_previous_observation"]["change_basis_points"] > 9.9
    assert "perspectives" not in rates_facts
    assert "http" not in rates_messages[-1].content
    for role, messages in gateway.messages.items():
        peer = "macro.risks" if role == "macro.rates" else "macro.rates"
        assert f"Independent {peer}" not in json.dumps([message.model_dump() for message in messages])
    report = registry.get(result.report_artifact_id)
    assert report.metadata["task_type"] == "macro.research"
    assert report.metadata["evidence_kind"] == "OPINION"
    assert report.metadata["approved_publication_hash"] is None
    assert transport.calls == ["DGS10"]


def test_macro_research_with_no_visible_data_abstains_locally(store, registry, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=OfflineTransport())
    collection = service.collect(MacroCollectRequest(series_ids=["DGS10"]))
    runtime = _runtime(store, registry, service, config=_config())
    run = runtime.submit(
        RunRequest(
            task_type="macro.research",
            payload={"snapshot_id": collection.snapshot_id},
            mode="HISTORICAL",
            knowledge_cutoff=FETCHED_AT - timedelta(seconds=1),
        )
    )
    runtime.execute(run.run_id)
    completed = runtime.get(run.run_id)
    assert completed.status == "COMPLETED", completed.error
    assert completed.result.outcome == "INSUFFICIENT_EVIDENCE"
    assert completed.result.payload["interpretation_status"] == "LOCAL_ABSTENTION_NO_DATA"
    assert all(view["claims"] == [] for view in completed.result.payload["perspectives"].values())
    assert completed.result.evidence == []
    assert completed.usage["calls_used"] == 0
    assert runtime.calls(run.run_id) == []


@pytest.mark.parametrize(
    "defect", ["unknown_id", "direction", "extra_url", "missing_claims", "duplicate_ids"]
)
def test_macro_research_rejects_invalid_schema_or_claim_references(store, registry, monkeypatch, defect):
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=OfflineTransport())
    collection = service.collect(MacroCollectRequest(series_ids=["DGS10"]))
    gateway = ViewGateway(defect=defect)
    runtime = _runtime(store, registry, service, config=_config(), gateway=gateway)
    run = runtime.submit(
        RunRequest(task_type="macro.research", payload={"snapshot_id": collection.snapshot_id})
    )
    runtime.execute(run.run_id)
    completed = runtime.get(run.run_id)
    assert completed.status == "FAILED"
    assert completed.result is None
    assert 2 <= len(gateway.records) <= 4
    assert completed.usage["calls_used"] == len(gateway.records)
    assert all(receipt.status == "INVALID" for receipt in runtime.calls(run.run_id))
    assert registry.find_by_type(ArtifactType.RESEARCH_REPORT) == []


def test_macro_research_one_repair_is_audited_and_charged_to_same_ledger(store, registry, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=OfflineTransport())
    collection = service.collect(MacroCollectRequest(series_ids=["DGS10"]))
    gateway = ViewGateway(defect="unknown_id", repair=True)
    runtime = _runtime(store, registry, service, config=_config(), gateway=gateway)
    run = runtime.submit(
        RunRequest(task_type="macro.research", payload={"snapshot_id": collection.snapshot_id})
    )
    runtime.execute(run.run_id)
    completed = runtime.get(run.run_id)
    assert completed.status == "COMPLETED", completed.error
    assert completed.usage["calls_used"] == 3
    receipts = runtime.calls(run.run_id)
    assert len(receipts) == 3
    assert sorted(receipt.sequence for receipt in receipts) == [1, 2, 3]
    invalid = next(receipt for receipt in receipts if receipt.status == "INVALID")
    repaired = next(receipt for receipt in receipts if receipt.repair_of is not None)
    assert repaired.status == "COMPLETED"
    assert repaired.role == invalid.role == "macro.rates"
    assert repaired.repair_of == invalid.call_id
    assert completed.result.publication.decision == "NOT_APPLICABLE"


def test_macro_research_children_share_one_root_call_budget(store, registry, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=OfflineTransport())
    collection = service.collect(MacroCollectRequest(series_ids=["DGS10"]))
    gateway = ViewGateway()
    runtime = _runtime(store, registry, service, config=_config(), gateway=gateway)
    budget = BudgetLimits(max_calls=2, max_tokens=64_000, max_parallel_calls=2)
    run = runtime.submit(
        RunRequest(
            plan=ResearchPlan(
                tasks=[
                    PlanTask(
                        task_id=name,
                        task_type="macro.research",
                        payload={"snapshot_id": collection.snapshot_id},
                        budget=budget,
                    )
                    for name in ("first", "second")
                ],
                budget=budget,
            ),
            budget=budget,
        )
    )
    runtime.execute(run.run_id)
    completed = runtime.get(run.run_id)
    assert completed.status == "FAILED"
    assert completed.usage["calls_used"] == len(gateway.records) == 2
    assert completed.usage["tokens_reserved"] == 0
    assert gateway.peak <= 2
    assert sum(len(runtime.calls(child_id)) for child_id in completed.children) == 2


def test_macro_research_without_routes_is_local_non_advice(store, registry, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=OfflineTransport())
    collection = service.collect(MacroCollectRequest(series_ids=["DGS10"]))
    runtime = _runtime(store, registry, service)
    run = runtime.submit(
        RunRequest(task_type="macro.research", payload={"snapshot_id": collection.snapshot_id})
    )
    runtime.execute(run.run_id)
    completed = runtime.get(run.run_id)
    assert completed.status == "COMPLETED", completed.error
    assert completed.result.outcome == "INSUFFICIENT_EVIDENCE"
    assert completed.result.payload["interpretation_status"] == "LOCAL_ABSTENTION_NO_ROUTES"
    assert completed.result.payload["facts"]["indicators"]["DGS10"]["latest_value"] == 4.1
    assert completed.result.publication.decision == "NOT_APPLICABLE"
    assert runtime.calls(run.run_id) == []


def test_macro_task_prefers_namespaced_routes_over_legacy_fallback(store, registry):
    config = _config()
    dedicated = config.roles["critic"].model_copy(update={"model": "dedicated-macro"})
    config = config.model_copy(update={"roles": {**config.roles, "macro.rates": dedicated}})
    service = MacroService(store, registry, transport=OfflineTransport())
    tasks = macro_tasks(service, config, store=store, registry=registry)
    research = next(task for task in tasks if task.descriptor.task_type == "macro.research")
    assert research.descriptor.roles == {"macro.rates": "macro.rates", "macro.risks": "critic"}


@pytest.mark.parametrize(
    "task_type,payload",
    [
        ("macro.collect", {"url": "https://example.invalid/unregistered"}),
        ("macro.analyze", {"snapshot_id": "snapshot", "url": "https://example.invalid"}),
        ("macro.research", {"snapshot_id": "snapshot", "knowledge_cutoff": FETCHED_AT.isoformat()}),
        ("macro.research", {"snapshot_id": "snapshot", "focus": "x" * 1001}),
    ],
)
def test_macro_tasks_reject_unregistered_inputs_before_execution(store, registry, task_type, payload):
    transport = OfflineTransport()
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=transport)
    runtime = _runtime(store, registry, service)
    with pytest.raises(QuantSystemError):
        runtime.submit(RunRequest(task_type=task_type, payload=payload))
    assert transport.calls == []


def test_macro_gold_facts_safe_quantity_and_usd_valuation_are_not_global_demand(store, registry, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    safe_url = "https://www.safe.gov.cn/safe/2026/0206/27116.html"
    table = (
        '<table><tr><th rowspan="2">Item</th><th colspan="2">2026.01</th>'
        '<th colspan="2">2026.02</th></tr><tr><th>100 million USD</th><th>100 million SDR</th>'
        '<th>100 million USD</th><th>100 million SDR</th></tr><tr><td rowspan="2">Gold</td>'
        "<td>3695.82</td><td>2674.51</td><td>3875.88</td><td>2819.30</td></tr>"
        '<tr><td colspan="2">7419\u4e07\u76ce\u53f8</td><td>7422\u4e07\u76ce\u53f8</td>'
        "<td>7422\u4e07\u76ce\u53f8</td></tr></table>"
    ).encode()

    class SafeTransport:
        def __init__(self):
            self.calls = []

        def get(self, url, *, params, timeout):
            assert not params
            assert timeout > 0
            self.calls.append(url)
            if url == "https://www.safe.gov.cn/safe/whcb/index.html":
                return f'<a href="{safe_url}">Official reserve assets (2026)</a>'.encode()
            assert url == safe_url
            return table

    transport = SafeTransport()
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=transport)
    collection = service.collect(
        MacroCollectRequest(series_ids=["CN_GOLD_RESERVES", "DFII10"], start_date=date(2026, 1, 1))
    )
    projected = macro_gold_facts(service, collection.snapshot_id, FETCHED_AT)
    items = {key: EvidenceItem.model_validate(value) for key, value in projected["items"].items()}
    assert set(items) == {"china_gold_reserves_quantity", "china_gold_reserves_value", "real_yield"}
    assert items["china_gold_reserves_quantity"].value == 7422
    assert "10,000 troy ounces" in items["china_gold_reserves_quantity"].definition
    assert "not global central bank demand" in items["china_gold_reserves_quantity"].definition
    assert items["china_gold_reserves_quantity"].source == "SAFE:CN_GOLD_RESERVES"
    assert items["china_gold_reserves_value"].value == 3875.88
    assert "USD 100 million" in items["china_gold_reserves_value"].definition
    assert "not a quantity or demand change" in items["china_gold_reserves_value"].definition
    assert items["real_yield"].value is None
    assert "no observation at cutoff" in items["real_yield"].definition
    assert "nominal_yield_10y" not in items
    assert "central_bank_demand" not in items
    assert len(transport.calls) == 2


def test_macro_tools_validate_scope_typed_fields_and_cancellation_before_service(
    store, registry, monkeypatch
):
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    transport = OfflineTransport()
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=transport)
    tasks = {
        task.descriptor.task_type: task
        for task in macro_tasks(service, _config(), store=store, registry=registry)
    }
    tools = macro_tools(service)
    executor = ToolExecutor()
    for tool in tools:
        executor.register(tool)

    class Context:
        knowledge_cutoff = FETCHED_AT
        cancelled = False

        def check(self):
            if self.cancelled:
                raise QuantSystemError("research cancelled")

        def advance(self, *_args, **_kwargs):
            self.check()

    context = Context()
    ctx = cast(RunContext, context)
    collect_descriptor = tasks["macro.collect"].descriptor
    research_descriptor = tasks["macro.research"].descriptor
    with pytest.raises(QuantSystemError, match="not allowed"):
        executor.invoke(ctx, "macro.rates", "macro.sources.collect", {}, research_descriptor)
    with pytest.raises(QuantSystemError, match="not allowed"):
        executor.invoke(ctx, "system", "macro.sources.collect", {}, research_descriptor)
    with pytest.raises(QuantSystemError, match="unknown research tool request fields"):
        executor.invoke(
            ctx, "system", "macro.sources.collect", {"url": "https://example.invalid"}, collect_descriptor
        )
    with pytest.raises(QuantSystemError, match="capability"):
        executor.invoke(
            ctx,
            "system",
            "macro.sources.collect",
            {},
            collect_descriptor.model_copy(update={"capabilities": []}),
        )
    with pytest.raises(QuantSystemError, match="unknown research tool request fields"):
        executor.invoke(
            ctx,
            "system",
            "macro.snapshot.read",
            {"snapshot_id": "snapshot", "focus": "injected"},
            research_descriptor,
        )
    context.cancelled = True
    with pytest.raises(QuantSystemError, match="cancelled"):
        tools[0].invoke(ctx, MacroCollectRequest(series_ids=["DGS10"]))
    assert transport.calls == []
    assert registry.find_by_type(ArtifactType.MACRO_SNAPSHOT) == []


def test_macro_read_tool_uses_only_snapshot_and_context_cutoff_not_agent_opinions(
    store, registry, monkeypatch
):
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    transport = OfflineTransport()
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=transport)
    collection = service.collect(MacroCollectRequest(series_ids=["DGS10"]))

    class Context:
        knowledge_cutoff = FETCHED_AT

        def check(self):
            pass

    read = next(tool for tool in macro_tools(service) if tool.tool_id == "macro.snapshot.read")
    assert set(read.request_type.model_fields) == {"snapshot_id"}
    request = read.request_type(snapshot_id=collection.snapshot_id)
    before = len(registry.list_all())
    facts = read.invoke(cast(RunContext, Context()), request)
    assert facts == service.analyze(collection.snapshot_id, knowledge_cutoff=FETCHED_AT)
    assert facts["missing"] == {}
    assert set(facts["indicators"]) == {"DGS10"}
    assert facts["knowledge_cutoff"] == FETCHED_AT.isoformat()
    assert "opinions" not in facts and "perspectives" not in facts
    assert len(registry.list_all()) == before
    assert transport.calls == ["DGS10"]


def test_macro_research_materialized_request_focus_and_non_advice_publication_schema():
    request = MacroResearchRequest(snapshot_id="snapshot", focus="x" * 1000)
    assert request.focus == "x" * 1000
    publication = Publication(decision="NOT_APPLICABLE", reason="No investment advice.")
    result = TaskResult(outcome="INSUFFICIENT_EVIDENCE", summary="Local abstention", publication=publication)
    assert result.publication.content_hash is None


def test_macro_adapter_has_no_sibling_imports_or_private_service_dependency():
    path = Path(__file__).resolve().parents[2] / "src/quant_system/research/modules/macro.py"
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert not (node.module or "").startswith(
                ("quant_system.gold", "quant_system.factors", "quant_system.research.modules")
            )
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "service"
        ):
            assert not node.attr.startswith("_")


def test_macro_gold_facts_precise_series_names_units_and_actual_snapshot_reference(
    store, registry, monkeypatch
):
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    transport = OfflineTransport()
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=transport)
    collection = service.collect(MacroCollectRequest(series_ids=["DGS10", "DFII10", "DTWEXBGS", "DEXCHUS"]))
    cutoff = FETCHED_AT + timedelta(seconds=1)
    projected = macro_gold_facts(service, collection.snapshot_id, cutoff)
    assert set(projected) == {"snapshot_id", "items", "evidence"}
    assert set(projected["items"]) == {
        "real_yield",
        "nominal_yield_10y",
        "broad_dollar_index",
        "usd_cny_ny_noon",
    }
    items = {key: EvidenceItem.model_validate(value) for key, value in projected["items"].items()}
    assert all(item.category == "macro" and item.value == 4.1 for item in items.values())
    real_yield = items["real_yield"]
    assert real_yield.source == "FRED:DFII10"
    assert "percent" in real_yield.definition
    assert "2026-10-16" in real_yield.definition
    assert FETCHED_AT.isoformat() in real_yield.definition
    assert "historical PIT not certified" in real_yield.definition
    assert "not ICE DXY" in items["broad_dollar_index"].definition
    assert "not PBOC central parity" in items["usd_cny_ny_noon"].definition
    assert "dollar_index" not in items and "central_bank_demand" not in items
    reference = EvidenceRef.model_validate(projected["evidence"][0])
    artifact = registry.get(collection.snapshot_id)
    assert reference.artifact_id == artifact.artifact_id
    assert reference.content_hash == artifact.content_hash
    assert reference.schema_version == artifact.schema_version == "macro-snapshot-v1"
    assert reference.available_at == collection.available_at
    assert reference.namespace == "macro"
    assert reference.kind == "FACT"
    assert reference.record_key is None
    assert validate_evidence(reference, store, registry, knowledge_cutoff=cutoff) == reference
    assert (
        macro_gold_facts(service, collection.snapshot_id, cutoff, store=store, registry=registry) == projected
    )
    assert transport.calls == ["DGS10", "DFII10", "DTWEXBGS", "DEXCHUS"]


def test_macro_gold_facts_reject_unavailable_snapshot_and_partial_dependency_injection(
    store, registry, monkeypatch
):
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=OfflineTransport())
    collection = service.collect(MacroCollectRequest(series_ids=["DFII10"]))
    with pytest.raises(ValueError, match="unavailable"):
        macro_gold_facts(service, collection.snapshot_id, FETCHED_AT - timedelta(seconds=1))
    with pytest.raises(ValueError, match="both store and registry"):
        macro_gold_facts(service, collection.snapshot_id, FETCHED_AT, store=store)
