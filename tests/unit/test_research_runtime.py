from datetime import UTC, datetime

import pytest

from quant_system.artifacts.envelope import ArtifactEnvelope
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.domain.enums import ArtifactType
from quant_system.domain.exceptions import QuantSystemError
from quant_system.research.runtime.calls import CallReceipt
from quant_system.research.runtime.evidence import EvidenceManifest, validate_evidence
from quant_system.research.runtime.models import EvidenceRef, ResearchEvent, ResearchRun, RunRequest
from quant_system.research.runtime.storage import ArtifactRunRepository


def runtime_for_test(store, registry, execute=None, *, clock=None, budget=None):
    from pydantic import BaseModel, ConfigDict

    from quant_system.agents.config import AgentLLMConfig
    from quant_system.research.runtime.engine import ResearchRuntime
    from quant_system.research.runtime.models import TaskDescriptor, TaskResult
    from quant_system.research.runtime.ports import ModuleRegistry, ResearchTask

    class Request(BaseModel):
        model_config = ConfigDict(extra="forbid")
        text: str = "sample"

    class OfflineGateway:
        def complete(self, *_args):
            raise AssertionError("this task must not call a model")

    modules = ModuleRegistry()
    descriptor = TaskDescriptor(
        task_type="documents.inspect",
        module_id="documents",
        title="Inspect",
        **({"default_budget": budget} if budget else {}),
    )
    modules.register(
        ResearchTask(
            descriptor,
            Request,
            execute
            or (
                lambda ctx, request: TaskResult(
                    outcome="REJECTED",
                    summary=request.text,
                    payload={"length": len(request.text)},
                )
            ),
        )
    )
    return ResearchRuntime(
        modules,
        ArtifactRunRepository(store, registry),
        AgentLLMConfig(providers={}, roles={}),
        OfflineGateway(),
        credentials_required=False,
        **({"clock": clock} if clock else {}),
    )


def test_nonfinancial_runtime_idempotency_and_duplicate_dispatch(store, registry):
    from quant_system.research.runtime.engine import ResearchConflict

    runtime = runtime_for_test(store, registry)
    request = RunRequest(task_type="documents.inspect", payload={"text": "offline"}, idempotency_key="key")
    submitted = runtime.submit(request)
    assert runtime.submit(request).run_id == submitted.run_id
    with pytest.raises(ResearchConflict, match="different request"):
        runtime.submit(request.model_copy(update={"payload": {"text": "different"}}))
    with pytest.raises(ResearchConflict, match="active"):
        runtime.submit(RunRequest(task_type="documents.inspect"))
    runtime.execute(submitted.run_id)
    runtime.execute(submitted.run_id)
    result = runtime.get(submitted.run_id)
    assert result.status == "COMPLETED"
    assert result.result.outcome == "REJECTED"
    assert result.result.payload == {"length": 7}
    assert runtime.status()["active_run_id"] is None
    assert len([event for event in runtime.events(result.run_id) if event.event_type == "started"]) == 1
    result.result.payload.clear()
    assert runtime.get(result.run_id).result.payload == {"length": 7}
    assert runtime.submit(request).run_id == result.run_id


def test_runtime_recovers_queued_as_interrupted_without_replay(store, registry):
    runtime = runtime_for_test(store, registry)
    run = runtime.submit(RunRequest(task_type="documents.inspect", idempotency_key="recover"))
    restarted = runtime_for_test(store, registry)
    assert restarted.get(run.run_id).status == "INTERRUPTED"
    before = len(registry.list_all())
    restarted.get(run.run_id)
    restarted.events(run.run_id)
    restarted.calls(run.run_id)
    restarted.status()
    assert len(registry.list_all()) == before
    restarted.execute(run.run_id)
    assert restarted.get(run.run_id).result is None
    assert restarted.submit(run.request).run_id == run.run_id


def test_explicit_entry_executes_its_registered_recipe_and_keeps_original_idempotency(store, registry):
    from quant_system.research.runtime.models import PlanTask, ResearchPlan
    from quant_system.research.runtime.ports import ResearchRecipe

    runtime = runtime_for_test(store, registry)
    runtime.module_registry.register_recipe(
        ResearchRecipe(
            "documents.pipeline",
            lambda _: False,
            lambda question, payload, budget: ResearchPlan(
                budget=budget,
                tasks=[
                    PlanTask(
                        task_id="first",
                        task_type="documents.inspect",
                        payload={"text": "first"},
                        budget=budget,
                    ),
                    PlanTask(
                        task_id="second",
                        task_type="documents.inspect",
                        payload=payload,
                        depends_on=["first"],
                        budget=budget,
                    ),
                ],
            ),
            ("documents.inspect",),
        )
    )
    request = RunRequest(task_type="documents.inspect", payload={"text": "second"}, idempotency_key="recipe")
    queued = runtime.submit(request)
    assert queued.request == request
    assert queued.execution_plan is not None
    assert queued.task_type == "runtime.plan"
    runtime.execute(queued.run_id)
    completed = runtime.get(queued.run_id)
    assert completed.status == "COMPLETED", completed.error
    assert len(completed.children) == 2
    assert [runtime.get(child).result.summary for child in completed.children] == ["first", "second"]
    assert runtime.submit(request).run_id == queued.run_id
    reopened = runtime_for_test(store, registry)
    assert reopened.get(queued.run_id).execution_plan == queued.execution_plan
    assert reopened.submit(request).run_id == queued.run_id


def test_cancel_keeps_active_slot_until_callable_exits(store, registry):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from quant_system.research.runtime.engine import ResearchConflict
    from quant_system.research.runtime.models import TaskResult

    entered, release = Event(), Event()

    def task(ctx, request):
        entered.set()
        assert release.wait(5)
        ctx.check()
        return TaskResult(outcome="OK", summary="never published")

    runtime = runtime_for_test(store, registry, task)
    run = runtime.submit(RunRequest(task_type="documents.inspect"))
    with ThreadPoolExecutor(max_workers=2) as workers:
        execution = workers.submit(runtime.execute, run.run_id)
        assert entered.wait(5)
        runtime.execute(run.run_id)
        assert runtime.cancel(run.run_id).cancel_requested
        assert runtime.status()["active_run_id"] == run.run_id
        with pytest.raises(ResearchConflict):
            runtime.submit(RunRequest(task_type="documents.inspect"))
        release.set()
        execution.result(timeout=5)
    assert runtime.get(run.run_id).status == "CANCELLED"
    assert runtime.get(run.run_id).result is None
    assert runtime.status()["active_run_id"] is None


def test_repository_revisions_calls_and_read_only_reload(
    store: LocalArtifactStore,
    registry: ArtifactRegistry,
) -> None:
    repository = ArtifactRunRepository(store, registry)
    run = ResearchRun(
        run_id="root",
        root_run_id="root",
        task_type="documents.inspect",
        module_id="documents",
        request=RunRequest(task_type="documents.inspect"),
    )
    saved = repository.save(run)
    repository.save(saved.model_copy(update={"stage": "working"}))
    event = ResearchEvent(run_id="root", sequence=1, event_type="stage", stage="working")
    repository.event(event)
    started = CallReceipt(
        call_id="root:call",
        sequence=1,
        role="reader",
        provider="fake",
        model="offline",
        status="STARTED",
        messages=[],
        reservation_tokens=20,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    repository.call("root", started)
    repository.call("root", started.model_copy(update={"status": "COMPLETED", "used_tokens": 10}))
    before = len(registry.list_all())
    reloaded = ArtifactRunRepository(store, registry)
    assert reloaded.load()[0].revision == 2
    assert reloaded.load()[0].stage == "working"
    assert reloaded.events("root") == [event]
    assert reloaded.calls("root")[0].status == "COMPLETED"
    assert reloaded.calls("root", offset=1) == []
    assert len(registry.find_by_type(ArtifactType.RESEARCH_CALL)) == 2
    assert len(registry.list_all()) == before
    assert (
        "messages" not in store.get_bytes(registry.find_by_type(ArtifactType.RESEARCH_RUN)[-1].uri).decode()
    )
    with pytest.raises(QuantSystemError, match="immutable"):
        reloaded.event(event.model_copy(update={"stage": "rewritten"}))


def test_evidence_checks_actual_availability_kind_and_raw_hash(store, registry) -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    earlier = datetime(2026, 1, 1, tzinfo=UTC)
    uri, digest = store.put_content_addressed_json("facts", {"available_at": now.isoformat(), "value": 7})
    artifact = registry.register(
        ArtifactEnvelope(
            artifact_type=ArtifactType.DATA_SNAPSHOT,
            producer="offline",
            uri=uri,
            content_hash=digest,
            created_at=now,
            metadata={"available_at": now.isoformat(), "kind": "OPINION"},
        )
    )
    ref = EvidenceRef(artifact_id=artifact.artifact_id, content_hash=digest, available_at=now, kind="OPINION")
    manifest = EvidenceManifest(store, registry, clock=lambda: now)
    assert manifest.add(ref, knowledge_cutoff=now) == ref
    assert manifest.snapshot(now)["references"][0]["content_hash"] == digest
    with pytest.raises(QuantSystemError, match="actual artifact"):
        validate_evidence(
            ref.model_copy(update={"available_at": earlier}), store, registry, knowledge_cutoff=now
        )
    with pytest.raises(QuantSystemError, match="knowledge cutoff"):
        validate_evidence(ref, store, registry, knowledge_cutoff=earlier)
    with pytest.raises(QuantSystemError, match="kind"):
        validate_evidence(ref.model_copy(update={"kind": "FACT"}), store, registry, knowledge_cutoff=now)
    from pathlib import Path

    Path(uri.removeprefix("file://")).write_bytes(b"corrupted")
    with pytest.raises(QuantSystemError, match="hash mismatch"):
        validate_evidence(ref, store, registry, knowledge_cutoff=now)


def llm_runtime(store, registry, execute, *, gateway=None, budget=None, clock=None):
    from pydantic import BaseModel, ConfigDict

    from quant_system.agents.config import AgentLLMConfig
    from quant_system.agents.llm import LLMResponse
    from quant_system.research.runtime.engine import ResearchRuntime
    from quant_system.research.runtime.models import TaskDescriptor
    from quant_system.research.runtime.ports import ModuleRegistry, ResearchTask

    class Request(BaseModel):
        model_config = ConfigDict(extra="forbid")
        text: str = "offline"

    class Gateway:
        def __init__(self):
            self.roles = []

        def complete(self, role, messages):
            self.roles.append(role)
            return LLMResponse(
                content='{"value": 7}', provider="fake", model="offline", input_tokens=5, output_tokens=3
            )

    gateway = gateway or Gateway()
    config = AgentLLMConfig.model_validate(
        {
            "providers": {
                "fake": {
                    "kind": "openai",
                    "base_url": "https://example.invalid",
                    "api_key_env": "UNUSED_RUNTIME_KEY",
                }
            },
            "roles": {"documents.reader": {"provider": "fake", "model": "offline", "max_output_tokens": 50}},
        }
    )
    modules = ModuleRegistry()
    modules.register(
        ResearchTask(
            TaskDescriptor(
                task_type="documents.inspect",
                module_id="documents",
                title="Inspect",
                roles={"reader": "documents.reader"},
                **({"default_budget": budget} if budget else {}),
            ),
            Request,
            execute,
        )
    )
    return ResearchRuntime(
        modules,
        ArtifactRunRepository(store, registry),
        config,
        gateway,
        credentials_required=False,
        **({"clock": clock} if clock else {}),
    ), gateway


@pytest.mark.parametrize("method", ["call", "complete"])
@pytest.mark.parametrize("problem", ["role", "unknown", "provider", "model", "tokens"])
def test_direct_task_caller_rejects_role_and_binding_bypass(store, registry, method, problem):
    from quant_system.agents.llm import LLMMessage
    from quant_system.research.runtime.models import TaskResult

    def execute(ctx, request):
        role = "reader"
        binding = runtime.config.roles["documents.reader"]
        if problem == "role":
            role, binding = "documents.reader", None
        elif problem == "unknown":
            role = "documents.undeclared"
        else:
            binding = binding.model_copy(
                update={
                    "provider": {"provider": "other"},
                    "model": {"model": "unapproved-model"},
                    "tokens": {"max_output_tokens": binding.max_output_tokens + 1},
                }[problem]
            )
        messages = [LLMMessage(role="user", content="must be refused")]
        if method == "call":
            ctx.caller.call(role, messages, dict[str, int], binding=binding)
        else:
            ctx.caller.complete(role, messages, binding=binding)
        return TaskResult(outcome="OK", summary="unreachable")

    runtime, gateway = llm_runtime(store, registry, execute)
    runtime.config = runtime.config.model_copy(
        update={
            "providers": {**runtime.config.providers, "other": runtime.config.providers["fake"]},
        }
    )
    run = runtime.submit(RunRequest(task_type="documents.inspect"))
    runtime.execute(run.run_id)
    result = runtime.get(run.run_id)
    assert result.status == "FAILED"
    error = "role is not declared" if problem in {"role", "unknown"} else "binding exceeds"
    assert error in result.error
    assert gateway.roles == runtime.calls(run.run_id) == []
    assert registry.find_by_type(ArtifactType.RESEARCH_CALL) == []
    assert result.usage["calls_used"] == result.usage["tokens_used"] == result.usage["tokens_reserved"] == 0


@pytest.mark.parametrize("method", ["context", "call", "complete"])
@pytest.mark.parametrize("explicit", [False, True])
def test_declared_logical_binding_ignores_unused_global_credentials(
    store,
    registry,
    monkeypatch,
    method,
    explicit,
):
    from quant_system.agents.llm import LLMMessage
    from quant_system.research.runtime.models import TaskResult

    monkeypatch.setenv("UNUSED_RUNTIME_KEY", "test-only")
    monkeypatch.delenv("MISSING_OTHER_RUNTIME_KEY", raising=False)

    def execute(ctx, request):
        configured = runtime.config.roles["documents.reader"]
        binding = configured.model_copy(update={"max_output_tokens": 20}) if explicit else None
        messages = [LLMMessage(role="user", content="count")]
        if method == "context":
            answer = ctx.call("reader", messages, dict[str, int], binding=binding)
        elif method == "call":
            answer = ctx.caller.call("reader", messages, dict[str, int], binding=binding)
        else:
            response = ctx.caller.complete("reader", messages, binding=binding)
            answer = ctx.caller.parse(response.content, dict[str, int])
        return TaskResult(outcome="OK", summary="Counted", payload=answer)

    runtime, gateway = llm_runtime(store, registry, execute)
    config = runtime.config
    runtime.config = config.model_copy(
        update={
            "providers": {
                **config.providers,
                "unused": config.providers["fake"].model_copy(
                    update={
                        "api_key_env": "MISSING_OTHER_RUNTIME_KEY",
                    }
                ),
            },
            "roles": {
                **config.roles,
                "unused.global": config.roles["documents.reader"].model_copy(
                    update={
                        "provider": "unused",
                        "model": "unused-model",
                    }
                ),
            },
        }
    )
    runtime.credentials_required = True
    before = runtime.config.model_dump()
    run = runtime.submit(RunRequest(task_type="documents.inspect"))
    runtime.execute(run.run_id)
    result = runtime.get(run.run_id)
    assert result.status == "COMPLETED", result.error
    assert result.result.payload == {"value": 7}
    assert gateway.roles == ["reader"]
    receipt = runtime.calls(run.run_id)[0]
    assert receipt.role == "reader"
    assert receipt.route == {"provider": "fake", "model": "offline"}
    assert result.usage["calls_used"] == 1
    assert result.usage["tokens_used"] == 8
    assert runtime.config.model_dump() == before


def test_calls_share_child_root_budget_and_keep_logical_roles(store, registry):
    from quant_system.agents.llm import LLMMessage
    from quant_system.research.runtime.calls import BudgetLimits
    from quant_system.research.runtime.models import PlanTask, ResearchPlan, TaskResult

    def execute(ctx, request):
        answer = ctx.call("reader", [LLMMessage(role="user", content="count")], dict[str, int])
        return TaskResult(outcome="OK", summary="Counted", payload=answer)

    runtime, gateway = llm_runtime(store, registry, execute)
    budget = BudgetLimits(max_calls=1, max_tokens=2000)
    plan = ResearchPlan(
        tasks=[
            PlanTask(task_id=name, task_type="documents.inspect", budget=budget)
            for name in ("first", "second")
        ],
        budget=budget,
    )
    run = runtime.submit(RunRequest(plan=plan, budget=budget))
    runtime.execute(run.run_id)
    result = runtime.get(run.run_id)
    assert result.status == "FAILED"
    assert result.result.outcome == "FAILED"
    assert result.usage["calls_used"] == 1
    assert gateway.roles == ["reader"]
    assert len(runtime.calls(run.run_id)) == 1
    assert all(runtime.get(child).request.plan is None for child in result.children)
    assert all(runtime.get(child).parent_run_id == run.run_id for child in result.children)
    assert sorted(runtime.get(child).status for child in result.children) == ["COMPLETED", "FAILED"]


def _registered_model_plan(runtime, *, now, budget=None, tasks=None, used_tokens=8):
    from quant_system.research.runtime.calls import BudgetLimits
    from quant_system.research.runtime.models import PlanTask, ResearchPlan

    limits = budget or BudgetLimits()
    plan = ResearchPlan(
        source="MODEL",
        budget=limits,
        tasks=tasks
        or [
            PlanTask(task_id="inspect", task_type="documents.inspect", budget=limits),
        ],
    )
    receipts = [
        CallReceipt(
            call_id=f"raw-planner-{sequence}",
            sequence=sequence,
            role="research.planner",
            provider="fake",
            model="offline",
            status="INVALID" if sequence == 1 else "COMPLETED",
            messages=[],
            reservation_tokens=100,
            used_tokens=used_tokens,
            created_at=now,
            repair_of="raw-planner-1" if sequence == 2 else None,
        )
        for sequence in (1, 2)
    ]
    artifact_id = runtime.repository.save_plan(plan, receipts)
    return plan, receipts, artifact_id


@pytest.mark.parametrize("change", ["unregistered", "payload", "source", "budget", "empty_receipts"])
def test_model_plan_must_match_authoritative_archive(store, registry, change):
    from quant_system.research.runtime.calls import BudgetLimits

    now = datetime(2026, 10, 17, tzinfo=UTC)
    runtime = runtime_for_test(store, registry, clock=lambda: now)
    plan, _, _ = _registered_model_plan(runtime, now=now)
    if change == "unregistered":
        plan = plan.model_copy(update={"plan_id": "unarchived-model-plan"})
    elif change == "payload":
        plan.tasks[0].payload["text"] = "tampered"
    elif change == "source":
        plan = plan.model_copy(update={"source": "EXPLICIT"})
    elif change == "budget":
        plan = plan.model_copy(update={"budget": BudgetLimits(max_calls=29)})
    else:
        plan = plan.model_copy(update={"plan_id": "model-without-receipts"})
        runtime.repository.save_plan(plan)
    before = len(registry.find_by_type(ArtifactType.RESEARCH_RUN))
    with pytest.raises(QuantSystemError, match="registered|planner receipts"):
        runtime.submit(RunRequest(plan=plan))
    assert runtime.status()["active_run_id"] is None
    assert len(registry.find_by_type(ArtifactType.RESEARCH_RUN)) == before


def test_explicit_unknown_and_saved_rule_plans_remain_readonly(store, registry):
    from quant_system.research.runtime.models import PlanTask, ResearchPlan

    runtime = runtime_for_test(store, registry)
    for source in ("EXPLICIT", "RULE"):
        plan = ResearchPlan(source=source, tasks=[PlanTask(task_id="one", task_type="documents.inspect")])
        if source == "RULE":
            artifact_id = runtime.repository.save_plan(plan)
            assert runtime.repository.plan_receipts(artifact_id) == []
        run = runtime.submit(RunRequest(plan=plan))
        runtime.execute(run.run_id)
        completed = runtime.get(run.run_id)
        assert completed.status == "COMPLETED", completed.error
        assert completed.usage["calls_used"] == completed.usage["tokens_used"] == 0
        assert runtime.calls(run.run_id) == []
    assert registry.find_by_type(ArtifactType.RESEARCH_CALL) == []


def test_planner_receipts_carry_once_with_stable_sequence_pagination_and_restart(store, registry):
    from quant_system.agents.llm import LLMMessage
    from quant_system.research.runtime.models import TaskResult

    now = datetime(2026, 10, 17, tzinfo=UTC)

    def execute(ctx, request):
        assert ctx.ledger.usage()["calls_used"] == 0
        answer = ctx.call("reader", [LLMMessage(role="user", content="count")], dict[str, int])
        return TaskResult(outcome="OK", summary="Counted", payload=answer)

    runtime, gateway = llm_runtime(store, registry, execute, clock=lambda: now)
    plan, history, artifact_id = _registered_model_plan(runtime, now=now)
    request = RunRequest(plan=runtime.repository.get_plan(artifact_id), idempotency_key="carry-once")
    run = runtime.submit(request)
    assert run.usage["planning_plan_id"] == plan.plan_id
    assert run.usage["calls_used"] == 2
    assert run.usage["tokens_used"] == 16
    assert runtime.submit(request) == run
    assert runtime.calls(run.run_id) == history
    assert registry.find_by_type(ArtifactType.RESEARCH_CALL) == []
    first_commit = next(
        item
        for item in registry.find_by_type(ArtifactType.RESEARCH_RUN)
        if item.metadata["run_id"] == run.run_id
    )
    assert artifact_id in first_commit.parent_artifact_ids
    runtime.execute(run.run_id)
    result = runtime.get(run.run_id)
    assert result.status == "COMPLETED", result.error
    assert gateway.roles == ["reader"]
    assert result.usage["calls_used"] == 3
    assert result.usage["tokens_used"] == 24
    assert [receipt.sequence for receipt in runtime.calls(run.run_id)] == [1, 2, 3]
    assert runtime.calls(run.run_id, offset=1, limit=1) == history[1:]
    child = runtime.get(result.children[0])
    assert child.usage["calls_used"] == 1
    assert child.usage["tokens_used"] == 8
    assert child.usage["root_usage"]["calls_used"] == 3
    assert len(runtime.calls(child.run_id)) == 1
    assert runtime.calls(run.run_id)[:2] == history
    assert {item.metadata["sequence"] for item in registry.find_by_type(ArtifactType.RESEARCH_CALL)} == {3}
    before = len(registry.list_all())
    restarted, _ = llm_runtime(store, registry, execute, clock=lambda: now)
    assert restarted.get(run.run_id).usage == result.usage
    assert restarted.calls(run.run_id) == runtime.calls(run.run_id)
    assert restarted.submit(request).run_id == run.run_id
    assert len(registry.list_all()) == before


@pytest.mark.parametrize("constraint", ["calls", "tokens"])
def test_precharged_planning_blocks_send_at_exhausted_root_budget(store, registry, constraint):
    from quant_system.agents.llm import LLMMessage
    from quant_system.research.runtime.calls import BudgetLimits
    from quant_system.research.runtime.models import TaskResult

    now = datetime(2026, 10, 17, tzinfo=UTC)
    limits = BudgetLimits(
        max_calls=2 if constraint == "calls" else 30, max_tokens=1000 if constraint == "calls" else 16
    )

    def execute(ctx, request):
        ctx.call("reader", [LLMMessage(role="user", content="blocked")], dict[str, int])
        return TaskResult(outcome="OK", summary="unreachable")

    runtime, gateway = llm_runtime(store, registry, execute, clock=lambda: now)
    plan, history, _ = _registered_model_plan(runtime, now=now, budget=limits)
    run = runtime.submit(RunRequest(plan=plan, budget=limits))
    runtime.execute(run.run_id)
    result = runtime.get(run.run_id)
    assert result.status == "FAILED"
    assert gateway.roles == []
    assert result.usage["calls_used"] == 2
    assert result.usage["tokens_used"] == 16
    assert runtime.calls(run.run_id) == history
    assert registry.find_by_type(ArtifactType.RESEARCH_CALL) == []


def test_planning_and_children_share_root_budget_without_reset(store, registry):
    from quant_system.agents.llm import LLMMessage
    from quant_system.research.runtime.calls import BudgetLimits
    from quant_system.research.runtime.models import PlanTask, TaskResult

    now = datetime(2026, 10, 17, tzinfo=UTC)
    limits = BudgetLimits(max_calls=3, max_tokens=2000)

    def execute(ctx, request):
        ctx.call("reader", [LLMMessage(role="user", content="count")], dict[str, int])
        return TaskResult(outcome="OK", summary="Counted")

    runtime, gateway = llm_runtime(store, registry, execute, clock=lambda: now)
    nodes = [
        PlanTask(task_id=name, task_type="documents.inspect", budget=limits) for name in ("first", "second")
    ]
    plan, _, _ = _registered_model_plan(runtime, now=now, budget=limits, tasks=nodes)
    run = runtime.submit(RunRequest(plan=plan, budget=limits))
    runtime.execute(run.run_id)
    result = runtime.get(run.run_id)
    assert result.status == "FAILED"
    assert gateway.roles == ["reader"]
    assert result.usage["calls_used"] == 3
    assert result.usage["tokens_used"] == 24
    assert sum(runtime.get(child).usage["calls_used"] for child in result.children) == 1
    assert sorted(runtime.get(child).status for child in result.children) == ["COMPLETED", "FAILED"]


@pytest.mark.parametrize("elapsed", [3, 10, 11])
def test_planning_elapsed_time_cannot_reset_root_deadline(store, registry, elapsed):
    from datetime import timedelta

    from quant_system.research.runtime.calls import BudgetLimits

    now = datetime(2026, 10, 17, tzinfo=UTC)
    limits = BudgetLimits(timeout_seconds=10)
    runtime = runtime_for_test(store, registry, clock=lambda: now)
    plan, _, _ = _registered_model_plan(runtime, now=now - timedelta(seconds=elapsed), budget=limits)
    if elapsed >= 10:
        with pytest.raises(QuantSystemError, match="planning deadline"):
            runtime.submit(RunRequest(plan=plan, budget=limits))
        assert runtime.list_runs() == []
    else:
        run = runtime.submit(RunRequest(plan=plan, budget=limits))
        assert runtime._ledgers[run.run_id].remaining_seconds() == 7
        assert run.usage["timeout_seconds"] == 7


def test_interrupted_root_recovery_adds_planning_and_unknown_execution_usage(store, registry):
    now = datetime(2026, 10, 17, tzinfo=UTC)
    runtime = runtime_for_test(store, registry, clock=lambda: now)
    plan, history, _ = _registered_model_plan(runtime, now=now)
    request = RunRequest(plan=plan, idempotency_key="interrupted-carry")
    run = runtime.submit(request)
    runtime.repository.call(
        run.run_id,
        CallReceipt(
            call_id=f"{run.run_id}:unfinished",
            sequence=3,
            role="reader",
            provider="fake",
            model="offline",
            status="STARTED",
            messages=[],
            reservation_tokens=37,
            created_at=now,
        ),
    )
    restarted = runtime_for_test(store, registry, clock=lambda: now)
    recovered = restarted.get(run.run_id)
    assert recovered.status == "INTERRUPTED"
    assert recovered.usage["calls_used"] == 3
    assert recovered.usage["tokens_used"] == 53
    assert recovered.usage["tokens_reserved"] == 0
    assert restarted.calls(run.run_id)[:2] == history
    assert restarted.calls(run.run_id)[2].status == "UNKNOWN"
    assert restarted.submit(request).run_id == run.run_id
    before = len(registry.list_all())
    restarted.recover()
    assert restarted.get(run.run_id).usage == recovered.usage
    assert len(registry.list_all()) == before


def test_controlled_planning_task_keeps_two_call_limit_and_execution_root_carries_raw_receipts(
    store, registry
):
    from quant_system.agents.llm import LLMMessage, LLMResponse
    from quant_system.research.runtime.calls import BudgetLimits
    from quant_system.research.runtime.models import PlanTask, ResearchPlan, TaskDescriptor, TaskResult
    from quant_system.research.runtime.ports import ResearchTask

    candidate = ResearchPlan(source="MODEL", tasks=[PlanTask(task_id="one", task_type="documents.inspect")])

    class Gateway:
        def __init__(self):
            self.responses = ["invalid JSON", candidate.model_dump_json(), '{"value": 7}']
            self.roles = []

        def complete(self, role, messages):
            self.roles.append(role)
            return LLMResponse(
                content=self.responses.pop(0),
                provider="fake",
                model="offline",
                input_tokens=5,
                output_tokens=3,
            )

    def inspect(ctx, request):
        answer = ctx.call("reader", [LLMMessage(role="user", content="count")], dict[str, int])
        return TaskResult(outcome="OK", summary="Counted", payload=answer)

    runtime, gateway = llm_runtime(store, registry, inspect, gateway=Gateway())

    def propose(ctx, request):
        proposed = ctx.call("reader", [LLMMessage(role="user", content="plan")], ResearchPlan)
        artifact_id = runtime.repository.save_plan(proposed, ctx.caller.receipts)
        return TaskResult(
            outcome="PROPOSED", summary="Preview only", payload={"plan_artifact_id": artifact_id}
        )

    runtime.module_registry.register(
        ResearchTask(
            TaskDescriptor(
                task_type="research.plan",
                module_id="research",
                title="Plan",
                roles={"reader": "documents.reader"},
                default_budget=BudgetLimits(max_calls=2),
            ),
            runtime.module_registry.get("documents.inspect").request_type,
            propose,
        )
    )
    preview = runtime.submit(RunRequest(task_type="research.plan"))
    runtime.execute(preview.run_id)
    planned = runtime.get(preview.run_id)
    assert planned.status == "COMPLETED", planned.error
    assert planned.usage["max_calls"] == planned.usage["calls_used"] == 2
    artifact_id = planned.result.payload["plan_artifact_id"]
    history = runtime.repository.plan_receipts(artifact_id)
    assert [receipt.sequence for receipt in history] == [1, 2]
    assert runtime.calls(preview.run_id)[0].call_id == f"{preview.run_id}:{history[0].call_id}"
    assert history[1].repair_of == history[0].call_id
    execution = runtime.submit(RunRequest(plan=runtime.repository.get_plan(artifact_id)))
    assert execution.usage["max_calls"] == 30
    assert execution.usage["calls_used"] == 2
    assert execution.usage["tokens_used"] == 16
    runtime.execute(execution.run_id)
    completed = runtime.get(execution.run_id)
    assert completed.status == "COMPLETED", completed.error
    assert completed.usage["calls_used"] == len(gateway.roles) == 3
    assert completed.usage["tokens_used"] == 24
    assert runtime.calls(execution.run_id)[:2] == history
    assert [receipt.sequence for receipt in runtime.calls(execution.run_id)] == [1, 2, 3]
    assert len(registry.find_by_type(ArtifactType.RESEARCH_CALL)) == 6


@pytest.mark.parametrize("timestamp", ["naive", "future"])
def test_planning_creation_time_must_be_aware_and_not_in_future(store, registry, timestamp):
    from datetime import timedelta

    now = datetime(2026, 10, 17, tzinfo=UTC)
    runtime = runtime_for_test(store, registry, clock=lambda: now)
    created_at = now.replace(tzinfo=None) if timestamp == "naive" else now + timedelta(seconds=1)
    plan, _, _ = _registered_model_plan(runtime, now=created_at)
    with pytest.raises(QuantSystemError, match="aware|future"):
        runtime.submit(RunRequest(plan=plan))
    assert runtime.list_runs() == []


@pytest.mark.parametrize("required", [True, False])
def test_plan_preserves_required_failure_or_optional_partial(store, registry, required):
    from quant_system.research.runtime.models import PlanTask, ResearchPlan, TaskResult

    def execute(ctx, request):
        if request.text == "fail":
            raise ValueError("offline failure")
        return TaskResult(
            outcome="INSUFFICIENT_EVIDENCE", summary="No advice", payload={"must_not_copy": "action"}
        )

    runtime = runtime_for_test(store, registry, execute)
    plan = ResearchPlan(
        tasks=[
            PlanTask(task_id="success", task_type="documents.inspect"),
            PlanTask(
                task_id="failure", task_type="documents.inspect", payload={"text": "fail"}, required=required
            ),
        ]
    )
    run = runtime.submit(RunRequest(plan=plan))
    runtime.execute(run.run_id)
    result = runtime.get(run.run_id)
    assert result.status == ("FAILED" if required else "COMPLETED")
    assert result.result.outcome == ("FAILED" if required else "PARTIAL")
    assert "must_not_copy" not in str(result.result.payload)
    assert result.result.publication.decision == "NOT_APPLICABLE"
    assert len(result.result.payload["summaries"]) == 2
    assert all(runtime.get(child).status not in {"QUEUED", "RUNNING"} for child in result.children)


def test_cancel_during_network_preserves_receipt_and_blocks_publication(store, registry):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from quant_system.agents.llm import LLMMessage, LLMResponse
    from quant_system.research.runtime.models import Publication, TaskResult

    entered, release = Event(), Event()

    class Gateway:
        def complete(self, role, messages):
            entered.set()
            assert release.wait(5)
            return LLMResponse(
                content='{"value": 7}', provider="fake", model="offline", input_tokens=5, output_tokens=3
            )

    def execute(ctx, request):
        ctx.call("reader", [LLMMessage(role="user", content="offline")], dict[str, int])
        return TaskResult(
            outcome="OK",
            summary="not publishable",
            publication=Publication(decision="PASS", content_hash="a" * 64),
        )

    runtime, _ = llm_runtime(store, registry, execute, gateway=Gateway())
    run = runtime.submit(RunRequest(task_type="documents.inspect"))
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(runtime.execute, run.run_id)
        assert entered.wait(5)
        assert runtime.calls(run.run_id)[0].status == "STARTED"
        assert runtime.get(run.run_id).usage["tokens_reserved"] > 0
        runtime.cancel(run.run_id)
        assert runtime.status()["busy"]
        release.set()
        future.result(timeout=5)
    result = runtime.get(run.run_id)
    assert result.status == "CANCELLED"
    assert result.result is None
    assert result.usage["calls_used"] == 1
    assert result.usage["tokens_reserved"] == 0
    assert runtime.calls(run.run_id)[0].status == "FAILED"
    assert runtime.calls(run.run_id)[0].call_id.startswith(f"{run.run_id}:")


def test_recovery_marks_started_call_unknown_and_charges_reservation(store, registry):
    runtime = runtime_for_test(store, registry)
    run = runtime.submit(RunRequest(task_type="documents.inspect"))
    runtime.repository.call(
        run.run_id,
        CallReceipt(
            call_id=f"{run.run_id}:unknown",
            sequence=1,
            role="reader",
            provider="fake",
            model="offline",
            status="STARTED",
            messages=[],
            reservation_tokens=37,
        ),
    )
    restarted = runtime_for_test(store, registry)
    assert restarted.get(run.run_id).status == "INTERRUPTED"
    assert restarted.calls(run.run_id)[0].status == "UNKNOWN"
    assert restarted.get(run.run_id).usage["tokens_used"] == 37
    assert restarted.get(run.run_id).usage["tokens_reserved"] == 0


def test_acquisition_precedes_one_shared_cutoff_for_all_analysis(store, registry):
    from datetime import timedelta
    from threading import Barrier

    from pydantic import BaseModel, ConfigDict

    from quant_system.research.runtime.models import (
        InputRef,
        PlanTask,
        ResearchPlan,
        TaskDescriptor,
        TaskResult,
    )
    from quant_system.research.runtime.ports import ResearchTask

    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    observed = []
    barrier = Barrier(2, timeout=5)

    class Request(BaseModel):
        model_config = ConfigDict(extra="forbid")
        snapshot_id: str

    def collect(ctx, request):
        assert ctx.knowledge_cutoff is None
        now[0] += timedelta(seconds=1)
        uri, digest = store.put_content_addressed_json("offline", {"value": 1})
        artifact = registry.register(
            ArtifactEnvelope(
                artifact_type=ArtifactType.DATA_SNAPSHOT,
                producer="offline",
                content_hash=digest,
                uri=uri,
                created_at=now[0],
                metadata={"available_at": now[0].isoformat()},
            )
        )
        reference = ctx.evidence(
            EvidenceRef(artifact_id=artifact.artifact_id, content_hash=digest, available_at=now[0])
        )
        return TaskResult(
            outcome="OK",
            summary="Collected",
            payload={"snapshot_id": artifact.artifact_id},
            evidence=[reference],
        )

    def analyze(ctx, request):
        assert ctx.knowledge_cutoff == now[0]
        observed.append(ctx.knowledge_cutoff)
        barrier.wait()
        return TaskResult(outcome="REJECTED", summary="No new advice")

    runtime = runtime_for_test(store, registry, clock=lambda: now[0])
    collect_request = runtime.module_registry.get("documents.inspect").request_type
    runtime.module_registry.register(
        ResearchTask(
            TaskDescriptor(
                task_type="documents.collect", module_id="documents", title="Collect", acquisition=True
            ),
            collect_request,
            collect,
            {"snapshot_id": str},
        )
    )
    runtime.module_registry.register(
        ResearchTask(
            TaskDescriptor(task_type="documents.analyze", module_id="documents", title="Analyze"),
            Request,
            analyze,
        )
    )
    plan = ResearchPlan(
        tasks=[
            PlanTask(task_id="collect", task_type="documents.collect"),
            *[
                PlanTask(
                    task_id=name,
                    task_type="documents.analyze",
                    depends_on=["collect"],
                    inputs={"snapshot_id": InputRef(task_id="collect", output_key="snapshot_id")},
                )
                for name in ("first", "second")
            ],
        ]
    )
    run = runtime.submit(RunRequest(plan=plan))
    runtime.execute(run.run_id)
    result = runtime.get(run.run_id)
    assert result.status == "COMPLETED", result.error
    assert observed == [now[0], now[0]]
    assert result.knowledge_cutoff == now[0]
    assert all(runtime.get(child).knowledge_cutoff == now[0] for child in result.children)
    assert len(result.usage["evidence_manifest"]["references"]) == 1


def test_direct_derived_task_starts_with_frozen_cutoff(store, registry):
    from quant_system.research.runtime.models import TaskResult

    now = datetime(2026, 1, 1, tzinfo=UTC)

    def execute(ctx, request):
        assert ctx.knowledge_cutoff == now
        return TaskResult(outcome="OK", summary="cutoff bound")

    runtime = runtime_for_test(store, registry, execute, clock=lambda: now)
    run = runtime.submit(RunRequest(task_type="documents.inspect"))
    runtime.execute(run.run_id)
    assert runtime.get(run.run_id).status == "COMPLETED"


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_stopped_plan_closes_pending_children_and_preserves_audit(store, registry, stop):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from threading import Event

    from quant_system.research.runtime.calls import BudgetLimits
    from quant_system.research.runtime.models import PlanTask, ResearchPlan, TaskResult

    entered, release = Event(), Event()
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    executions = []

    def execute(ctx, request):
        executions.append(request.text)
        if request.text == "block":
            entered.set()
            assert release.wait(5)
        return TaskResult(outcome="OK", summary=request.text, payload={"raw": "not aggregated"})

    runtime = runtime_for_test(store, registry, execute, clock=lambda: now[0])
    limits = BudgetLimits(timeout_seconds=5)
    plan = ResearchPlan(
        budget=limits,
        tasks=[
            PlanTask(task_id="first", task_type="documents.inspect", payload={"text": "done"}, budget=limits),
            PlanTask(
                task_id="blocked",
                task_type="documents.inspect",
                payload={"text": "block"},
                depends_on=["first"],
                budget=limits,
            ),
            PlanTask(
                task_id="pending",
                task_type="documents.inspect",
                payload={"text": "must not execute"},
                depends_on=["blocked"],
                budget=limits,
            ),
        ],
    )
    run = runtime.submit(RunRequest(plan=plan, budget=limits))
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(runtime.execute, run.run_id)
        assert entered.wait(5)
        if stop == "cancel":
            runtime.cancel(run.run_id)
        else:
            now[0] += timedelta(seconds=6)
        release.set()
        future.result(timeout=5)
    result = runtime.get(run.run_id)
    assert result.status == ("CANCELLED" if stop == "cancel" else "FAILED")
    assert all(runtime.get(child).status not in {"QUEUED", "RUNNING"} for child in result.children)
    assert executions == ["done", "block"]
    assert result.result.payload["approved_publication_hashes"] == []
    assert result.result.payload["summaries"][0]["outcome"] == "OK"
    assert "raw" not in result.result.payload
    assert runtime.status()["active_run_id"] is None


def test_shared_model_concurrency_is_at_most_three_with_stable_root_sequences(store, registry):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock

    from quant_system.agents.llm import LLMMessage, LLMResponse
    from quant_system.research.runtime.calls import BudgetLimits
    from quant_system.research.runtime.models import PlanTask, ResearchPlan, TaskResult

    three_entered, release = Event(), Event()
    lock = Lock()
    active = 0
    peak = 0

    class Gateway:
        def complete(self, role, messages):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 3:
                    three_entered.set()
            assert release.wait(5)
            with lock:
                active -= 1
            return LLMResponse(
                content='{"value": 7}', provider="fake", model="offline", input_tokens=5, output_tokens=3
            )

    def execute(ctx, request):
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(ctx.call, "reader", [LLMMessage(role="user", content="offline")], dict[str, int])
                for _ in range(4)
            ]
            for future in futures:
                future.result(timeout=5)
        return TaskResult(outcome="OK", summary="Counted")

    runtime, _ = llm_runtime(store, registry, execute, gateway=Gateway())
    budget = BudgetLimits(max_calls=8, max_tokens=10000, max_parallel_calls=8)
    plan = ResearchPlan(
        budget=budget,
        tasks=[
            PlanTask(task_id=name, task_type="documents.inspect", budget=budget)
            for name in ("first", "second")
        ],
    )
    run = runtime.submit(RunRequest(plan=plan, budget=budget))
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(runtime.execute, run.run_id)
        assert three_entered.wait(5)
        assert runtime.get(run.run_id).usage["calls_used"] == 3
        release.set()
        future.result(timeout=10)
    result = runtime.get(run.run_id)
    assert result.status == "COMPLETED", result.error
    assert peak == 3
    assert [receipt.sequence for receipt in runtime.calls(run.run_id)] == list(range(1, 9))
    assert result.usage["calls_used"] == 8
    assert result.usage["tokens_used"] == 64
    assert result.usage["tokens_reserved"] == 0
    events = runtime.events(run.run_id, limit=1000)
    assert [event.at for event in events] == sorted(event.at for event in events)
    for owner in {event.run_id for event in events}:
        sequences = [event.sequence for event in events if event.run_id == owner]
        assert sequences == list(range(1, len(sequences) + 1))


def test_pass_publication_hash_is_preserved_without_hashing_entire_adapter_payload(store, registry):
    from quant_system.research.runtime.models import PlanTask, Publication, ResearchPlan, TaskResult

    approved = "a" * 64
    uri, digest = store.put_content_addressed_json("reports", {"diagnostic": "separate body"})
    report = registry.register(
        ArtifactEnvelope(
            artifact_type=ArtifactType.RESEARCH_REPORT,
            producer="offline",
            uri=uri,
            content_hash=digest,
            metadata={"approved_publication_hash": approved},
        )
    )

    def execute(ctx, request):
        return TaskResult(
            outcome="OK",
            summary="Approved by module",
            payload={"diagnostic": "separate body"},
            report_artifact_id=report.artifact_id,
            publication=Publication(decision="PASS", content_hash=approved),
        )

    runtime = runtime_for_test(store, registry, execute)
    run = runtime.submit(
        RunRequest(plan=ResearchPlan(tasks=[PlanTask(task_id="one", task_type="documents.inspect")]))
    )
    runtime.execute(run.run_id)
    result = runtime.get(run.run_id)
    assert result.status == "COMPLETED"
    assert result.result.payload["approved_publication_hashes"] == [approved]
    child = runtime.get(result.children[0])
    assert child.result.publication.content_hash == approved
    assert "diagnostic" not in result.result.payload


@pytest.mark.parametrize("problem", ["no_report", "missing", "null", "conflict", "kind", "FAILED"])
def test_root_cannot_publish_child_pass_without_valid_report_authorization(store, registry, problem):
    from quant_system.domain.enums import ArtifactStatus
    from quant_system.research.runtime.models import PlanTask, Publication, ResearchPlan, TaskResult

    approved = "a" * 64
    metadata = {"approved_publication_hash": approved}
    if problem == "missing":
        metadata = {}
    elif problem == "null":
        metadata = {"approved_publication_hash": None}
    elif problem == "conflict":
        metadata["publication_hash"] = "b" * 64
    uri, digest = store.put_content_addressed_json("reports", {"body": "audit only"})
    report = registry.register(
        ArtifactEnvelope(
            artifact_type=ArtifactType.DATA_SNAPSHOT if problem == "kind" else ArtifactType.RESEARCH_REPORT,
            producer="offline",
            uri=uri,
            content_hash=digest,
            metadata=metadata,
            status=ArtifactStatus.FAILED if problem == "FAILED" else ArtifactStatus.REGISTERED,
        )
    )

    def execute(ctx, request):
        return TaskResult(
            outcome="OK",
            summary="Untrusted pass",
            payload={"body": "must remain audit only"},
            report_artifact_id=None if problem == "no_report" else report.artifact_id,
            publication=Publication(decision="PASS", content_hash=approved),
        )

    runtime = runtime_for_test(store, registry, execute)
    plan = ResearchPlan(tasks=[PlanTask(task_id="one", task_type="documents.inspect")])
    run = runtime.submit(RunRequest(plan=plan))
    runtime.execute(run.run_id)
    root = runtime.get(run.run_id)
    assert root.status == "FAILED"
    assert root.result.publication.decision == "NOT_APPLICABLE"
    assert root.result.payload["approved_publication_hashes"] == []
    assert "must remain audit only" not in str(root.result)
    child = runtime.get(root.children[0])
    assert child.result is None
    assert "authorization" in child.error
    received = next(
        event for event in runtime.events(run.run_id) if event.event_type == "task_result_received"
    )
    assert received.data["result"]["payload"]["body"] == "must remain audit only"


def test_parent_preserves_opinion_kind_and_cannot_upgrade_withheld_output(store, registry):
    from quant_system.research.runtime.models import PlanTask, Publication, ResearchPlan, TaskResult

    now = datetime(2026, 10, 17, tzinfo=UTC)
    uri, digest = store.put_content_addressed_json("opinions", {"kind": "OPINION", "body": "rejected"})
    opinion = registry.register(
        ArtifactEnvelope(
            artifact_type=ArtifactType.RESEARCH_REPORT,
            created_at=now,
            producer="offline",
            uri=uri,
            content_hash=digest,
            metadata={"kind": "OPINION"},
        )
    )
    reference = EvidenceRef(
        artifact_id=opinion.artifact_id, content_hash=digest, available_at=now, kind="OPINION"
    )

    def execute(ctx, request):
        return TaskResult(
            outcome="REJECTED",
            summary="Withheld by module",
            payload={"body": "unapproved body"},
            publication=Publication(decision="WITHHELD", content_hash="a" * 64),
            evidence=[reference],
            report_artifact_id=opinion.artifact_id,
        )

    runtime = runtime_for_test(store, registry, execute, clock=lambda: now)
    run = runtime.submit(
        RunRequest(
            plan=ResearchPlan(
                tasks=[
                    PlanTask(task_id="one", task_type="documents.inspect"),
                ]
            )
        )
    )
    runtime.execute(run.run_id)
    result = runtime.get(run.run_id)
    assert result.status == "COMPLETED", result.error
    assert result.result.publication.decision == "NOT_APPLICABLE"
    assert result.result.payload["approved_publication_hashes"] == []
    assert result.result.payload["summaries"][0]["approved_publication_hash"] is None
    assert "unapproved body" not in str(result.result)
    assert result.result.evidence == [reference]
    assert result.usage["evidence_manifest"]["references"][0]["kind"] == "OPINION"
    with pytest.raises(QuantSystemError, match="kind"):
        validate_evidence(
            reference.model_copy(update={"kind": "FACT"}), store, registry, knowledge_cutoff=now
        )


def test_directory_evidence_and_ancestry_are_verified_at_cutoff(store, registry, tmp_path):
    from datetime import timedelta

    now = datetime(2026, 10, 17, tzinfo=UTC)
    later = now + timedelta(seconds=1)
    directory = tmp_path / "frozen-evidence"
    directory.mkdir()
    source = directory / "source.json"
    source.write_text('{"value": 7}', encoding="utf-8")
    digest = store.hash_dir(str(directory))
    artifact = registry.register(
        ArtifactEnvelope(
            artifact_type=ArtifactType.DATA_SNAPSHOT,
            created_at=now,
            producer="offline",
            uri=directory.as_uri(),
            content_hash=digest,
        )
    )
    reference = EvidenceRef(artifact_id=artifact.artifact_id, content_hash=digest, available_at=now)
    assert validate_evidence(reference, store, registry, knowledge_cutoff=now) == reference
    future_parent = registry.register(
        artifact.model_copy(update={"artifact_id": "future-directory", "created_at": later})
    )
    uri, content_hash = store.put_content_addressed_json("derived", {"available_at": now.isoformat()})
    derived = registry.register(
        ArtifactEnvelope(
            artifact_type=ArtifactType.DATA_SNAPSHOT,
            created_at=now,
            producer="offline",
            uri=uri,
            content_hash=content_hash,
            parent_artifact_ids=(future_parent.artifact_id,),
        )
    )
    with pytest.raises(QuantSystemError, match="parent.*knowledge cutoff"):
        validate_evidence(
            EvidenceRef(artifact_id=derived.artifact_id, content_hash=content_hash, available_at=now),
            store,
            registry,
            knowledge_cutoff=now,
        )
    source.write_text('{"value": 8}', encoding="utf-8")
    with pytest.raises(QuantSystemError, match="directory hash mismatch"):
        validate_evidence(reference, store, registry, knowledge_cutoff=now)


def test_report_publication_authorization_mismatch_fails_closed(store, registry):
    from quant_system.research.runtime.models import Publication, TaskResult

    uri, digest = store.put_content_addressed_json("reports", {"body": "authorized by adapter"})
    report = registry.register(
        ArtifactEnvelope(
            artifact_type=ArtifactType.RESEARCH_REPORT,
            producer="offline",
            uri=uri,
            content_hash=digest,
            metadata={"approved_publication_hash": "b" * 64},
        )
    )

    def execute(ctx, request):
        return TaskResult(
            outcome="OK",
            summary="Incorrect authorization",
            report_artifact_id=report.artifact_id,
            publication=Publication(decision="PASS", content_hash="a" * 64),
        )

    runtime = runtime_for_test(store, registry, execute)
    run = runtime.submit(RunRequest(task_type="documents.inspect"))
    runtime.execute(run.run_id)
    result = runtime.get(run.run_id)
    assert result.status == "FAILED"
    assert "authorization" in result.error


def test_deadline_uses_task_default_and_smaller_root_limit(store, registry):
    from datetime import timedelta

    from quant_system.research.runtime.calls import BudgetLimits
    from quant_system.research.runtime.models import TaskResult

    now = [datetime(2026, 1, 1, tzinfo=UTC)]

    def execute(ctx, request):
        assert ctx.ledger.limits.max_calls == 0
        assert ctx.ledger.remaining_seconds() == 2
        now[0] += timedelta(seconds=3)
        return TaskResult(outcome="OK", summary="too late")

    runtime = runtime_for_test(
        store, registry, execute, clock=lambda: now[0], budget=BudgetLimits(max_calls=0, timeout_seconds=2)
    )
    run = runtime.submit(RunRequest(task_type="documents.inspect", budget=BudgetLimits(timeout_seconds=4)))
    runtime.execute(run.run_id)
    result = runtime.get(run.run_id)
    assert result.status == "FAILED"
    assert "deadline" in result.error
    assert result.usage["max_calls"] == 0


def test_future_raw_parent_cannot_be_hidden_behind_earlier_derived_availability(store, registry):
    earlier = datetime(2026, 1, 1, tzinfo=UTC)
    later = datetime(2026, 1, 2, tzinfo=UTC)
    raw_uri, raw_hash = store.put_content_addressed_json("raw", {"value": 7})
    raw = registry.register(
        ArtifactEnvelope(
            artifact_type=ArtifactType.DATA_SNAPSHOT,
            producer="offline",
            uri=raw_uri,
            content_hash=raw_hash,
            created_at=later,
            metadata={"available_at": later.isoformat()},
        )
    )
    uri, digest = store.put_content_addressed_json("derived", {"raw_snapshot_id": raw.artifact_id})
    derived = registry.register(
        ArtifactEnvelope(
            artifact_type=ArtifactType.DATA_SNAPSHOT,
            producer="offline",
            uri=uri,
            content_hash=digest,
            parent_artifact_ids=(raw.artifact_id,),
            metadata={"available_at": earlier.isoformat()},
        )
    )
    reference = EvidenceRef(artifact_id=derived.artifact_id, content_hash=digest, available_at=earlier)
    with pytest.raises(QuantSystemError, match="parent.*knowledge cutoff"):
        validate_evidence(reference, store, registry, knowledge_cutoff=earlier)


def test_refused_result_body_is_kept_in_audit_not_published(store, registry):
    from quant_system.research.runtime.models import Publication, TaskResult

    def execute(ctx, request):
        return TaskResult(
            outcome="OK",
            summary="Invalid hash",
            payload={"body": "preserve for audit"},
            publication=Publication(decision="PASS", content_hash="not-a-sha256"),
        )

    runtime = runtime_for_test(store, registry, execute)
    run = runtime.submit(RunRequest(task_type="documents.inspect"))
    runtime.execute(run.run_id)
    assert runtime.get(run.run_id).status == "FAILED"
    assert runtime.get(run.run_id).result is None
    event = next(event for event in runtime.events(run.run_id) if event.event_type == "task_result_received")
    assert event.data["result"]["payload"] == {"body": "preserve for audit"}
