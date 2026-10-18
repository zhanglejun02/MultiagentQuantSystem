from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Event
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from quant_system.agents.config import AgentLLMConfig
from quant_system.agents.llm import LLMMessage, LLMResponse
from quant_system.api.research import PlanRequest, register_research_routes
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.domain.enums import ArtifactType
from quant_system.domain.exceptions import QuantSystemError
from quant_system.research.runtime.engine import ResearchConflict, ResearchRuntime, RunContext
from quant_system.research.runtime.models import RunRequest, TaskDescriptor, TaskResult
from quant_system.research.runtime.ports import ModuleRegistry, ResearchTask
from quant_system.research.runtime.storage import ArtifactRunRepository


class DocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(default="offline", min_length=1, max_length=100)


class OfflineGateway:
    def complete(self, *_args):
        raise AssertionError("offline research must not call a model")


def _node(task_id="document", *, task_type="documents.inspect", **changes):
    return {"task_id": task_id, "task_type": task_type, **changes}


def _plan(*nodes, **changes):
    return {"tasks": list(nodes) or [_node()], **changes}


def _config(*, planner_role="research.planner") -> AgentLLMConfig:
    return AgentLLMConfig.model_validate(
        {
            "providers": {
                "fake": {
                    "kind": "openai_compatible",
                    "base_url": "https://example.invalid",
                    "api_key_env": "RESEARCH_API_TEST_KEY",
                }
            },
            "roles": {
                role: {"provider": "fake", "model": f"offline-{role}", "max_output_tokens": 64}
                for role in (planner_role, "documents.reader")
            },
        }
    )


class FakeGateway:
    def __init__(self, *responses):
        self.responses = list(responses) or [_plan()]
        self.calls = []
        self.before_call = None

    def complete(self, role, messages):
        if self.before_call is not None:
            self.before_call(role, messages)
        self.calls.append((role, list(messages)))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return LLMResponse(
            content=response if isinstance(response, str) else json.dumps(response),
            provider="fake",
            model=f"offline-{role}",
            input_tokens=10,
            output_tokens=5,
            raw_request_id=f"offline-{len(self.calls)}",
        )


@pytest.fixture
def api_factory(store: LocalArtifactStore, registry: ArtifactRegistry):
    with ExitStack() as stack:

        def build(*, config=None, gateway=None, credentials_required=True, planning_task=None):
            executions = []

            def inspect(context: RunContext, request: BaseModel) -> TaskResult:
                document = DocumentRequest.model_validate(request.model_dump())
                executions.append(document.text)
                context.advance("inspecting")
                return TaskResult(
                    outcome="INSPECTED", summary=document.text, payload={"length": len(document.text)}
                )

            def review(context: RunContext, request: BaseModel) -> TaskResult:
                document = DocumentRequest.model_validate(request.model_dump())
                context.advance("reviewing")
                answer = context.call(
                    "reader", [LLMMessage(role="user", content=document.text)], dict[str, int]
                )
                executions.append(document.text)
                return TaskResult(outcome="REVIEWED", summary=document.text, payload=answer)

            modules = ModuleRegistry()
            modules.register(
                ResearchTask(
                    TaskDescriptor(
                        task_type="documents.inspect", module_id="documents", title="Inspect document"
                    ),
                    DocumentRequest,
                    inspect,
                )
            )
            modules.register(
                ResearchTask(
                    TaskDescriptor(
                        task_type="documents.review",
                        module_id="documents",
                        title="Review document",
                        roles={"reader": "documents.reader"},
                    ),
                    DocumentRequest,
                    review,
                )
            )
            if planning_task is not None:
                modules.register(planning_task)
            config = config or AgentLLMConfig(providers={}, roles={})
            gateway = gateway or OfflineGateway()
            runtime = ResearchRuntime(
                modules,
                ArtifactRunRepository(store, registry),
                config,
                gateway,
                credentials_required=credentials_required,
            )
            app = FastAPI()
            register_research_routes(app, runtime, config, gateway)
            client = stack.enter_context(TestClient(app))
            return client, runtime, executions

        yield build


@pytest.fixture
def research_api(api_factory):
    return api_factory()


def test_rule_plan_is_archived_preview_until_explicit_submission(research_api, registry):
    client, runtime, executions = research_api
    before = len(registry.list_all())
    response = client.post(
        "/api/research/plans", json={"question": "documents.inspect", "payload": {"text": "preview"}}
    )
    assert response.status_code == 200
    proposed = response.json()
    assert proposed["plan"]["source"] == "RULE"
    assert proposed["receipts"] == []
    artifact = registry.get(proposed["artifact_id"])
    assert artifact.artifact_type == ArtifactType.RESEARCH_PLAN
    assert runtime.repository.get_plan(artifact.artifact_id).model_dump(mode="json") == proposed["plan"]
    assert runtime.repository.plan_receipts(artifact.artifact_id) == []
    assert len(registry.list_all()) == before + 1
    assert client.get("/api/research/runs").json() == []
    assert executions == []
    assert runtime.status()["active_run_id"] is None

    submitted = client.post("/api/research/runs", json={"plan": proposed["plan"]})
    assert submitted.status_code == 202
    run = client.get(f"/api/research/runs/{submitted.json()['run_id']}").json()
    assert run["status"] == "COMPLETED"
    assert len(run["children"]) == 1
    assert executions == ["preview"]


def test_reads_paginate_roots_and_include_child_events_and_calls(api_factory, registry):
    gateway = FakeGateway({"length": 3}, {"length": 4})
    client, runtime, executions = api_factory(config=_config(), gateway=gateway, credentials_required=False)
    response = client.post(
        "/api/research/runs",
        json={
            "plan": _plan(
                _node("first", task_type="documents.review", payload={"text": "one"}),
                _node("second", task_type="documents.review", payload={"text": "next"}, depends_on=["first"]),
            )
        },
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    root = client.get(f"/api/research/runs/{run_id}").json()
    assert root["status"] == "COMPLETED"
    assert root["result"]["outcome"] == "COMPLETED"
    assert len(root["children"]) == 2
    children = [client.get(f"/api/research/runs/{child}").json() for child in root["children"]]
    assert all(child["status"] == "COMPLETED" for child in children)
    assert all(child["parent_run_id"] == child["root_run_id"] == run_id for child in children)
    assert root["usage"]["calls_used"] == 2
    assert root["usage"]["tokens_used"] == 30
    assert root["usage"]["tokens_reserved"] == 0
    assert all(child["usage"]["calls_used"] == 1 for child in children)
    assert executions == ["one", "next"]

    calls = client.get(f"/api/research/runs/{run_id}/calls").json()
    assert [call["sequence"] for call in calls] == [1, 2]
    assert all(call["status"] == "COMPLETED" for call in calls)
    assert all(call["used_tokens"] == 15 for call in calls)
    assert client.get(f"/api/research/runs/{run_id}/calls?offset=1&limit=1").json() == calls[1:2]
    assert client.get(f"/api/research/runs/{root['children'][0]}/calls").json() == calls[:1]
    events = client.get(f"/api/research/runs/{run_id}/events").json()
    assert {event["run_id"] for event in events} == {run_id, *root["children"]}
    assert client.get(f"/api/research/runs/{run_id}/events?offset=2&limit=3").json() == events[2:5]
    assert client.get(f"/api/research/runs/{run_id}/events?limit=0").json() == []
    assert all(
        event["run_id"] == root["children"][0]
        for event in client.get(f"/api/research/runs/{root['children'][0]}/events").json()
    )

    another = client.post("/api/research/runs", json={"task_type": "documents.inspect"}).json()
    before = len(registry.list_all())
    roots = client.get("/api/research/runs").json()
    assert [run["run_id"] for run in roots] == [another["run_id"], run_id]
    assert client.get("/api/research/runs?offset=1&limit=1").json() == roots[1:2]
    assert client.get("/api/research/runs?offset=2").json() == []
    assert client.get("/api/research/runs?limit=0").json() == []
    client.get("/api/research/modules")
    client.get(f"/api/research/runs/{run_id}")
    client.get(f"/api/research/runs/{run_id}/calls")
    client.get(f"/api/research/runs/{run_id}/events")
    assert len(registry.list_all()) == before


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"task_type": "documents.inspect", "plan": _plan()},
        {"task_type": "unknown.inspect"},
        {"task_type": "documents.inspect", "payload": {"text": 3}},
        {"task_type": "documents.inspect", "payload": {"text": "x" * 101}},
        {"task_type": "documents.inspect", "payload": {"unknown": True}},
        {"task_type": "documents.inspect", "budget": {"max_calls": -1}},
        {"task_type": "documents.inspect", "unknown": True},
        {"plan": {"tasks": []}},
        {"plan": _plan(*[_node(f"node{index}") for index in range(7)])},
        {"plan": _plan(_node("same"), _node("same"))},
        {"plan": _plan(_node("first", depends_on=["second"]), _node("second", depends_on=["first"]))},
        {"plan": _plan(_node(depends_on=["missing"]))},
        {"plan": _plan(_node(task_type="unknown.inspect"))},
        {"plan": _plan(_node(task_type="research.plan", payload={"question": "Inspect"}))},
        {"plan": _plan(_node(payload={"plan": _plan()}))},
        {"plan": _plan(_node(payload={"text": "x"}), task_budget={})},
        {"plan": _plan(), "payload": {"text": "cannot override plan"}},
        {"task_type": "research.plan", "payload": {"question": "test", "plan": _plan()}},
        {"task_type": "research.plan", "payload": {"question": "test", "payload": {"tasks": []}}},
    ],
)
def test_invalid_run_requests_fail_with_422_without_execution(research_api, payload, registry):
    client, runtime, executions = research_api
    before = len(registry.list_all())
    response = client.post("/api/research/runs", json=payload)
    assert response.status_code == 422
    assert "detail" in response.json()
    assert "Traceback" not in response.text
    assert executions == []
    assert runtime.status()["active_run_id"] is None
    assert len(registry.list_all()) == before


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": ""},
        {"question": "   "},
        {"question": "x" * 1001},
        {"question": "documents.inspect", "payload": []},
        {"question": "documents.inspect", "payload": {"text": 3}},
        {"question": "documents.inspect", "payload": {"tasks": []}},
        {"question": "documents.inspect", "budget": {"max_tokens": -1}},
        {"question": "documents.inspect", "budget": {"hidden_retry": 1}},
        {"question": "documents.inspect", "use_model": "true"},
        {"question": "documents.inspect", "use_model": 1},
        {"question": "unknown.inspect"},
        {"question": "documents.inspect", "extra": True},
    ],
)
def test_invalid_plan_requests_fail_with_422(research_api, payload, registry):
    client, runtime, executions = research_api
    before = len(registry.list_all())
    response = client.post("/api/research/plans", json=payload)
    assert response.status_code == 422
    assert "detail" in response.json()
    assert executions == []
    assert runtime.list_runs() == []
    assert len(registry.list_all()) == before


@pytest.mark.parametrize("suffix", ["", "/events", "/calls", "/cancel"])
def test_unknown_run_is_404(research_api, suffix):
    client, _, _ = research_api
    request = client.post if suffix == "/cancel" else client.get
    response = request(f"/api/research/runs/missing{suffix}")
    assert response.status_code == 404
    assert response.json()["detail"] == "missing"


@pytest.mark.parametrize("query", ["offset=-1", "offset=no", "limit=-1", "limit=1001", "limit=1.5"])
@pytest.mark.parametrize("suffix", ["", "/missing/events", "/missing/calls"])
def test_pagination_is_validated_by_pydantic(research_api, query, suffix):
    client, _, _ = research_api
    response = client.get(f"/api/research/runs{suffix}?{query}")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][0] == "query"


def test_completed_idempotent_submission_does_not_schedule_again(research_api, monkeypatch):
    client, runtime, executions = research_api
    execute = Mock(wraps=runtime.execute)
    monkeypatch.setattr(runtime, "execute", execute)
    request = {
        "task_type": "documents.inspect",
        "payload": {"text": "once"},
        "idempotency_key": "document-once",
    }
    first = client.post("/api/research/runs", json=request)
    repeated = client.post("/api/research/runs", json=request)
    assert first.status_code == repeated.status_code == 202
    assert first.json()["run_id"] == repeated.json()["run_id"]
    assert repeated.json()["status"] == "COMPLETED"
    execute.assert_called_once_with(first.json()["run_id"])
    assert executions == ["once"]
    different = client.post("/api/research/runs", json={**request, "payload": {"text": "different"}})
    assert different.status_code == 409
    assert "different request" in different.json()["detail"]
    assert execute.call_count == 1


def test_running_duplicate_does_not_dispatch_and_cancellation_keeps_slot(api_factory, monkeypatch):
    entered, release = Event(), Event()
    gateway = FakeGateway({"length": 7})

    def wait_for_release(*_args):
        entered.set()
        assert release.wait(5)

    gateway.before_call = wait_for_release
    client, runtime, executions = api_factory(config=_config(), gateway=gateway, credentials_required=False)
    execute = Mock(wraps=runtime.execute)
    monkeypatch.setattr(runtime, "execute", execute)
    request = RunRequest(task_type="documents.review", idempotency_key="running-once")
    queued = runtime.submit(request)
    with ThreadPoolExecutor(max_workers=1) as workers:
        execution = workers.submit(runtime.execute, queued.run_id)
        try:
            assert entered.wait(5)
            duplicate = client.post("/api/research/runs", json=request.model_dump(mode="json"))
            assert duplicate.status_code == 202
            assert duplicate.json()["run_id"] == queued.run_id
            assert duplicate.json()["status"] == "RUNNING"
            assert execute.call_count == 1
            conflict = client.post("/api/research/runs", json={"task_type": "documents.inspect"})
            assert conflict.status_code == 409
            cancelled = client.post(f"/api/research/runs/{queued.run_id}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["cancel_requested"]
            assert cancelled.json()["status"] == "RUNNING"
            assert runtime.status()["active_run_id"] == queued.run_id
            assert runtime.status()["cancel_requested"]
            assert (
                client.post("/api/research/runs", json={"task_type": "documents.inspect"}).status_code == 409
            )
        finally:
            release.set()
            execution.result(timeout=5)
    finished = client.get(f"/api/research/runs/{queued.run_id}").json()
    assert finished["status"] == "CANCELLED"
    assert finished["result"] is None
    assert runtime.status()["active_run_id"] is None
    assert executions == []
    assert len(gateway.calls) == 1
    execute.assert_called_once_with(queued.run_id)


def test_cancel_queued_run_without_background_dispatch(research_api):
    client, runtime, executions = research_api
    queued = runtime.submit(RunRequest(task_type="documents.inspect"))
    response = client.post(f"/api/research/runs/{queued.run_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert response.json()["cancel_requested"]
    assert runtime.status()["active_run_id"] is None
    runtime.execute(queued.run_id)
    assert executions == []
    assert client.post(f"/api/research/runs/{queued.run_id}/cancel").json() == response.json()
    events = client.get(f"/api/research/runs/{queued.run_id}/events").json()
    assert any(event["event_type"] == "cancel_requested" for event in events)
    assert not any(event["event_type"] == "started" for event in events)


@pytest.mark.parametrize("planner_role", ["research.planner", "orchestrator"])
def test_model_preview_archives_request_and_receipts_before_gateway(
    api_factory, store, registry, planner_role
):
    gateway = FakeGateway(_plan(_node(payload={"text": "proposed"})))
    config = _config(planner_role=planner_role)
    client, runtime, executions = api_factory(config=config, gateway=gateway, credentials_required=False)
    budget = {"max_calls": 8, "max_tokens": 90_000, "timeout_seconds": 180, "max_parallel_calls": 2}
    registered = runtime.module_registry.describe()

    def before_send(role, messages):
        assert role == "research.planner"
        assert messages and not executions
        archived = ArtifactRunRepository(store, registry)
        runs = archived.load()
        assert len(runs) == 1
        run = runs[0]
        assert run.task_type == "research.plan"
        assert run.status == "RUNNING" and run.stage == "planning"
        assert run.request.payload["question"] == "Inspect a document"
        assert run.request.payload["budget"] == budget
        assert run.usage["max_calls"] == 2
        assert run.usage["max_tokens"] == 30_000
        assert run.usage["calls_used"] == 1
        assert run.usage["tokens_reserved"] > 0
        assert runtime.status()["active_run_id"] == run.run_id
        receipts = archived.calls(run.run_id)
        assert len(receipts) == 1 and receipts[0].status == "STARTED"
        assert receipts[0].messages == messages
        with pytest.raises(ResearchConflict):
            runtime.submit(RunRequest(task_type="documents.inspect"))

    gateway.before_call = before_send
    response = client.post(
        "/api/research/plans", json={"question": "Inspect a document", "use_model": True, "budget": budget}
    )
    assert response.status_code == 200
    proposed = response.json()
    assert set(proposed) == {"plan", "artifact_id", "receipts"}
    assert proposed["plan"]["source"] == "MODEL"
    assert proposed["plan"]["budget"] == budget
    assert proposed["plan"]["question"] == "Inspect a document"
    assert proposed["plan"]["tasks"][0]["payload"] == {"text": "proposed"}
    assert executions == []
    assert len(gateway.calls) == 1
    assert runtime.module_registry.describe() == registered
    assert runtime.status()["active_run_id"] is None
    run = runtime.list_runs()[0]
    assert run.task_type == "research.plan" and run.status == "COMPLETED"
    assert run.children == []
    assert run.result.outcome == "PROPOSED"
    assert run.result.summary == "Inspect a document"
    assert run.result.payload == {"plan": proposed["plan"], "artifact_id": proposed["artifact_id"]}
    assert proposed["receipts"] == [receipt.model_dump(mode="json") for receipt in runtime.calls(run.run_id)]
    assert run.usage["calls_used"] == 1 and run.usage["tokens_used"] == 15
    assert run.usage["tokens_reserved"] == 0
    stored = runtime.repository.get_plan(proposed["artifact_id"])
    assert stored.model_dump(mode="json") == proposed["plan"]
    assert len(runtime.repository.plan_receipts(stored.plan_id)) == 1
    descriptor = runtime.module_registry.get("research.plan").describe()
    assert descriptor.roles == {"research.planner": planner_role}
    assert descriptor.request_schema["additionalProperties"] is False
    assert descriptor.default_budget.max_calls == 2
    assert descriptor.default_budget.max_tokens == 30_000


def test_model_planning_respects_global_slot_and_missing_roles(research_api):
    client, runtime, _ = research_api
    request = {"question": "Inspect a document", "use_model": True}
    missing = client.post("/api/research/plans", json=request)
    assert missing.status_code == 422
    assert "role is not configured: orchestrator" in missing.json()["detail"]
    queued = runtime.submit(RunRequest(task_type="documents.inspect"))
    busy = client.post("/api/research/plans", json=request)
    assert busy.status_code == 409
    assert runtime.get(queued.run_id).status == "QUEUED"
    assert len(runtime.list_runs()) == 1
    assert client.post(f"/api/research/runs/{queued.run_id}/cancel").status_code == 200


@pytest.mark.parametrize("entry", ["plan", "task", "review"])
def test_missing_credentials_return_503_without_gateway_calls(api_factory, monkeypatch, entry):
    monkeypatch.delenv("RESEARCH_API_TEST_KEY", raising=False)
    gateway = FakeGateway()
    client, runtime, executions = api_factory(config=_config(), gateway=gateway)
    if entry == "plan":
        response = client.post("/api/research/plans", json={"question": "Inspect", "use_model": True})
    else:
        response = client.post(
            "/api/research/runs",
            json={
                "task_type": "research.plan" if entry == "task" else "documents.review",
                "payload": {"question": "Inspect"} if entry == "task" else {},
            },
        )
    assert response.status_code == 503
    assert "missing LLM credential" in response.json()["detail"]
    assert runtime.list_runs() == []
    assert executions == gateway.calls == []


@pytest.mark.parametrize(
    ("responses", "expected_status", "receipt_status", "call_count"),
    [
        (("invalid JSON", "still invalid"), 422, "INVALID", 2),
        ((RuntimeError("provider failed"),), 422, "FAILED", 1),
        ((QuantSystemError("missing LLM credential: provider unavailable"),), 503, "FAILED", 1),
    ],
)
def test_failed_model_planning_keeps_audit_and_returns_typed_error(
    api_factory, registry, responses, expected_status, receipt_status, call_count
):
    gateway = FakeGateway(*responses)
    client, runtime, executions = api_factory(config=_config(), gateway=gateway, credentials_required=False)
    response = client.post("/api/research/plans", json={"question": "Inspect", "use_model": True})
    assert response.status_code == expected_status
    assert "detail" in response.json()
    assert "Traceback" not in response.text
    run = runtime.list_runs()[0]
    assert run.status == "FAILED" and run.result is None
    assert run.children == [] and executions == []
    assert run.usage["max_calls"] == 2 and run.usage["max_tokens"] == 30_000
    assert run.usage["calls_used"] == len(gateway.calls) == call_count
    assert run.usage["tokens_reserved"] == 0
    receipts = runtime.calls(run.run_id)
    assert len(receipts) == call_count
    assert all(receipt.status == receipt_status for receipt in receipts)
    if call_count == 2:
        assert receipts[1].repair_of == receipts[0].call_id
    assert registry.find_by_type(ArtifactType.RESEARCH_PLAN) == []
    assert runtime.status()["active_run_id"] is None


@pytest.mark.parametrize("budget", [{"max_calls": 0}, {"max_tokens": 0}])
def test_planning_respects_lower_requested_budget_before_gateway(api_factory, budget):
    gateway = FakeGateway()
    client, runtime, _ = api_factory(config=_config(), gateway=gateway, credentials_required=False)
    response = client.post(
        "/api/research/plans", json={"question": "Inspect", "use_model": True, "budget": budget}
    )
    assert response.status_code == 422
    assert gateway.calls == []
    run = runtime.list_runs()[0]
    assert run.status == "FAILED"
    assert run.usage["calls_used"] == 0
    assert runtime.calls(run.run_id) == []


def test_direct_planning_task_does_not_require_use_model_flag(api_factory):
    gateway = FakeGateway()
    client, runtime, executions = api_factory(config=_config(), gateway=gateway, credentials_required=False)
    response = client.post(
        "/api/research/runs", json={"task_type": "research.plan", "payload": {"question": "Inspect"}}
    )
    assert response.status_code == 202
    run = client.get(f"/api/research/runs/{response.json()['run_id']}").json()
    assert run["status"] == "COMPLETED"
    assert run["result"]["outcome"] == "PROPOSED"
    assert run["result"]["payload"]["plan"]["source"] == "MODEL"
    assert run["children"] == []
    assert len(gateway.calls) == 1 and executions == []


@pytest.mark.parametrize(
    "candidate",
    [
        _plan(_node(task_type="unknown.inspect")),
        _plan(_node("first", depends_on=["second"]), _node("second", depends_on=["first"])),
        _plan(*[_node(f"node{index}") for index in range(7)]),
        _plan(_node(payload={"plan": _plan()})),
        _plan(_node(task_type="research.plan", payload={"question": "Inspect again"})),
    ],
)
def test_invalid_model_plan_is_not_saved_or_executed(api_factory, registry, candidate):
    gateway = FakeGateway(candidate, candidate)
    client, runtime, executions = api_factory(config=_config(), gateway=gateway, credentials_required=False)
    response = client.post("/api/research/plans", json={"question": "Inspect", "use_model": True})
    assert response.status_code == 422
    run = runtime.list_runs()[0]
    assert run.status == "FAILED"
    assert run.children == [] and executions == []
    assert 1 <= len(gateway.calls) <= 2
    assert registry.find_by_type(ArtifactType.RESEARCH_PLAN) == []
    assert runtime.status()["active_run_id"] is None


def test_internal_planner_cannot_run_as_child_even_with_direct_runtime_submission(api_factory):
    gateway = FakeGateway()
    _, runtime, executions = api_factory(config=_config(), gateway=gateway, credentials_required=False)
    request = RunRequest.model_validate(
        {"plan": _plan(_node(task_type="research.plan", payload={"question": "Inspect"}))}
    )
    queued = runtime.submit(request)
    runtime.execute(queued.run_id)
    root = runtime.get(queued.run_id)
    assert root.status == "FAILED"
    assert len(root.children) == 1
    assert "root-only" in runtime.get(root.children[0]).error
    assert gateway.calls == executions == []


def test_registration_preserves_explicit_internal_handler(api_factory):
    calls = []

    def prepared(context: RunContext, request: BaseModel) -> TaskResult:
        calls.append(request.model_dump())
        return TaskResult(outcome="PROPOSED", summary="Registered at composition root")

    task = ResearchTask(
        TaskDescriptor(task_type="research.plan", module_id="research", title="Existing planner"),
        PlanRequest,
        prepared,
    )
    client, runtime, _ = api_factory(planning_task=task)
    assert runtime.module_registry.get("research.plan") is task
    response = client.post(
        "/api/research/runs", json={"task_type": "research.plan", "payload": {"question": "Inspect"}}
    )
    assert response.status_code == 202
    assert len(calls) == 1
    assert runtime.get(response.json()["run_id"]).result.summary == "Registered at composition root"


def test_schema_validation_error_is_422_without_non_json_context(research_api, monkeypatch):
    client, runtime, _ = research_api
    with pytest.raises(ValidationError) as caught:
        PlanRequest(question="")
    monkeypatch.setattr(runtime, "submit", Mock(side_effect=caught.value))
    response = client.post("/api/research/runs", json={"task_type": "documents.inspect"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["question"]
    assert "input" not in detail[0] and "ctx" not in detail[0]


def test_error_messages_redact_configured_credentials(api_factory, monkeypatch):
    secret = "offline-test-secret-not-a-real-key"
    monkeypatch.setenv("RESEARCH_API_TEST_KEY", secret)
    gateway = FakeGateway(RuntimeError(f"provider failed with {secret}"))
    client, runtime, _ = api_factory(config=_config(), gateway=gateway)
    response = client.post("/api/research/plans", json={"question": "Inspect", "use_model": True})
    assert response.status_code == 422
    assert secret not in response.text
    assert "[REDACTED]" in response.json()["detail"]
    assert secret not in runtime.get(runtime.list_runs()[0].run_id).error


def test_unexpected_programming_errors_are_not_disguised_as_validation(research_api, monkeypatch):
    client, runtime, _ = research_api
    monkeypatch.setattr(runtime, "status", Mock(side_effect=RuntimeError("programming error")))
    with pytest.raises(RuntimeError, match="programming error"):
        client.get("/api/research/modules")


def test_old_endpoints_are_not_registered_here(research_api):
    client, _, _ = research_api
    assert client.get("/api/gold/research/status").status_code == 404
    assert client.get("/api/agent-research/status").status_code == 404


def test_nonfinancial_module_and_background_run_need_no_keys(research_api):
    client, runtime, executions = research_api
    response = client.get("/api/research/modules")
    assert response.status_code == 200
    status = response.json()
    descriptors = {task["task_type"]: task for task in status["modules"]}
    assert status["ready"] and not status["busy"]
    assert descriptors["documents.inspect"]["ready"]
    assert not descriptors["research.plan"]["ready"]
    assert descriptors["research.plan"]["missing_roles"] == ["orchestrator"]

    submitted = client.post(
        "/api/research/runs", json={"task_type": "documents.inspect", "payload": {"text": "sample"}}
    )
    assert submitted.status_code == 202
    assert submitted.json()["status"] == "QUEUED"
    run_id = submitted.json()["run_id"]
    detail = client.get(f"/api/research/runs/{run_id}")
    assert detail.status_code == 200
    run = detail.json()
    assert run["status"] == "COMPLETED"
    assert run["result"]["outcome"] == "INSPECTED"
    assert run["result"]["payload"] == {"length": 6}
    assert run["usage"]["calls_used"] == run["usage"]["tokens_used"] == 0
    assert executions == ["sample"]
    assert client.get("/api/research/runs").json() == [run]
    assert client.get(f"/api/research/runs/{run_id}/calls").json() == []
    events = client.get(f"/api/research/runs/{run_id}/events").json()
    assert [event["event_type"] for event in events].count("started") == 1
    assert "inspecting" in [event["stage"] for event in events]
    assert events[-1]["data"]["usage"]["calls_used"] == 0
    assert runtime.status()["active_run_id"] is None
