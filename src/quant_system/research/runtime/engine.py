from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Any, TypeVar

from pydantic import BaseModel

from quant_system.agents.config import AgentLLMConfig, AgentRoleConfig
from quant_system.agents.llm import CompletionGateway, LLMMessage
from quant_system.artifacts.envelope import hash_json
from quant_system.domain.exceptions import ArtifactNotFoundError, QuantSystemError
from quant_system.domain.types import new_id, require_aware, utc_now
from quant_system.research.runtime.calls import BudgetLedger, BudgetLimits, CallReceipt, StructuredCaller
from quant_system.research.runtime.evidence import EvidenceManifest, verify_artifact
from quant_system.research.runtime.models import (
    EvidenceRef,
    PlanTask,
    ResearchEvent,
    ResearchPlan,
    ResearchRun,
    RunRequest,
    TaskResult,
)
from quant_system.research.runtime.planning import single_task_plan, validate_output, validate_request
from quant_system.research.runtime.ports import ModuleRegistry, ResearchTask
from quant_system.research.runtime.storage import ArtifactRunRepository, request_hash
from quant_system.research.runtime.tools import ToolExecutor

Result = TypeVar("Result")
_ACTIVE = {"QUEUED", "RUNNING"}


class ResearchConflict(QuantSystemError):
    status_code = 409


def _limits(*budgets: BudgetLimits) -> BudgetLimits:
    return BudgetLimits(
        max_calls=min(item.max_calls for item in budgets),
        max_tokens=min(item.max_tokens for item in budgets),
        timeout_seconds=min(item.timeout_seconds for item in budgets),
        max_parallel_calls=min(3, *(item.max_parallel_calls for item in budgets)),
    )


@dataclass
class _RootState:
    ledger: BudgetLedger
    manifest: EvidenceManifest
    cutoff: datetime | None
    event_sequence: int = 0
    acquiring: set[str] = field(default_factory=set)


class RunContext:
    def __init__(
        self,
        runtime: ResearchRuntime,
        run_id: str,
        task: ResearchTask,
        ledger: BudgetLedger,
        caller: StructuredCaller,
    ) -> None:
        self._runtime = runtime
        self._task = task
        self.run_id = run_id
        self.root_run_id = runtime._runs[run_id].root_run_id
        self.ledger = ledger
        self.caller = caller
        self.clock = runtime.clock

    @property
    def knowledge_cutoff(self) -> datetime | None:
        with self._runtime._lock:
            return self._runtime._states[self.root_run_id].cutoff

    def check(self) -> None:
        self._runtime._check(self.run_id)
        self.ledger.check()

    def advance(self, stage: str, **data: Any) -> None:
        self.check()
        self._runtime._advance(self.run_id, stage, data)

    def freeze_cutoff(self, cutoff: datetime | None = None) -> datetime:
        self.check()
        return self._runtime._freeze(self.run_id, cutoff)

    def evidence(self, reference: EvidenceRef) -> EvidenceRef:
        self.check()
        cutoff = self.knowledge_cutoff
        if cutoff is None and not self._task.descriptor.acquisition:
            cutoff = self.freeze_cutoff()
        state = self._runtime._states[self.root_run_id]
        checked = state.manifest.add(reference, knowledge_cutoff=cutoff)
        self._runtime._manifests[self.run_id].add(checked, knowledge_cutoff=cutoff)
        self.check()
        with self._runtime._lock:
            self._runtime._persist(self.run_id)
            if self.run_id != self.root_run_id:
                self._runtime._persist(self.root_run_id)
        return checked.model_copy(deep=True)

    def call(
        self,
        role: str,
        messages: list[LLMMessage],
        response_type: type[Result],
        *,
        binding: AgentRoleConfig | None = None,
        validator: Callable[[Result], None] | None = None,
        purpose: str = "",
        repair_limit: int = 1,
    ) -> Result:
        self.check()
        route = self._task.descriptor.roles.get(role)
        if route is None:
            raise QuantSystemError(f"role is not declared by this task: {role}")
        provider, configured = self._runtime.config.route(route)
        if binding is not None and (
            binding.provider != configured.provider
            or binding.model != configured.model
            or binding.max_output_tokens > configured.max_output_tokens
        ):
            raise QuantSystemError("call binding exceeds the registered role binding")
        if self._runtime.credentials_required:
            provider.api_key()
        if not self._task.descriptor.acquisition and self.knowledge_cutoff is None:
            self.freeze_cutoff()
        result = self.caller.call(
            role,
            messages,
            response_type,
            binding=binding or configured,
            validator=validator,
            purpose=purpose,
            repair_limit=repair_limit,
        )
        self.check()
        return result

    def tool(self, role: str, tool_id: str, payload: dict[str, Any]) -> Any:
        return self._runtime.tools.invoke(self, role, tool_id, payload, self._task.descriptor)


class ResearchRuntime:
    def __init__(
        self,
        module_registry: ModuleRegistry,
        repository: ArtifactRunRepository,
        config: AgentLLMConfig,
        gateway: CompletionGateway,
        *,
        clock: Callable[[], datetime] = utc_now,
        credentials_required: bool = True,
    ) -> None:
        self.module_registry = module_registry
        self.repository = repository
        self.config = config
        self.gateway = gateway
        self.clock = clock
        self.credentials_required = credentials_required
        self.tools = ToolExecutor()
        self._lock = RLock()
        self._runs: dict[str, ResearchRun] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._active_root: str | None = None
        self._executing: set[str] = set()
        self._states: dict[str, _RootState] = {}
        self._ledgers: dict[str, BudgetLedger] = {}
        self._manifests: dict[str, EvidenceManifest] = {}
        self.recover()

    def _now(self) -> datetime:
        return require_aware(self.clock())

    def _error(self, error: BaseException) -> str:
        import os

        message = str(error) or type(error).__name__
        secrets = {
            os.environ.get(provider.api_key_env, "").strip() for provider in self.config.providers.values()
        }
        for secret in sorted(secrets - {""}, key=len, reverse=True):
            message = message.replace(secret, "[REDACTED]")
        return message[:4000]

    def _manifest(self) -> EvidenceManifest:
        return EvidenceManifest(self.repository.store, self.repository.registry, clock=self.clock)

    def _persist(self, run_id: str, **changes: Any) -> ResearchRun:
        run = self._runs[run_id].model_copy(update=changes, deep=True)
        state = self._states.get(run.root_run_id)
        usage = dict(run.usage)
        if run_id in self._ledgers:
            ledger = self._ledgers[run_id]
            usage.update(ledger.usage())
            usage.update(
                {
                    "timeout_seconds": ledger.limits.timeout_seconds,
                    "max_parallel_calls": ledger.limits.max_parallel_calls,
                }
            )
        if state is not None:
            manifest = state.manifest if run_id == run.root_run_id else self._manifests.get(run_id)
            if manifest is not None:
                usage["evidence_manifest"] = manifest.snapshot(state.cutoff)
            if run_id != run.root_run_id:
                usage["root_usage"] = state.ledger.usage()
            run = run.model_copy(update={"knowledge_cutoff": state.cutoff})
        usage["cancel_requested"] = run.cancel_requested
        run = run.model_copy(update={"updated_at": self._now(), "usage": usage}, deep=True)
        saved = self.repository.save(run)
        self._runs[run_id] = saved
        return saved.model_copy(deep=True)

    def _event(self, run_id: str, event_type: str, **data: Any) -> None:
        run = self._runs[run_id]
        self.repository.event(
            run_id,
            event_type,
            run.stage,
            data={
                **data,
                "usage": deepcopy(run.usage),
                "cancel_requested": run.cancel_requested,
            },
        )

    def _check(self, run_id: str) -> None:
        with self._lock:
            run = self._runs[run_id]
            if run.cancel_requested or self._runs[run.root_run_id].cancel_requested:
                raise QuantSystemError("research cancelled")
            if run.status not in _ACTIVE:
                raise QuantSystemError("research run is no longer active")

    def _advance(self, run_id: str, stage: str, data: dict[str, Any]) -> None:
        if not isinstance(stage, str) or not stage:
            raise QuantSystemError("research stage must be a nonempty string")
        with self._lock:
            self._check(run_id)
            self._ledgers[run_id].check()
            event_type = str(data.pop("event_type", "stage"))
            self._persist(run_id, stage=stage)
            root_id = self._runs[run_id].root_run_id
            if root_id != run_id:
                self._persist(root_id)
            self._event(run_id, event_type, **data)

    def _freeze(self, run_id: str, cutoff: datetime | None = None) -> datetime:
        with self._lock:
            self._check(run_id)
            run = self._runs[run_id]
            state = self._states[run.root_run_id]
            state.ledger.check()
            if state.cutoff is not None:
                if cutoff is not None and require_aware(cutoff) != state.cutoff:
                    raise QuantSystemError("root knowledge cutoff is immutable")
                return state.cutoff
            if state.acquiring - {run_id}:
                raise QuantSystemError("cannot freeze cutoff before all acquisition tasks finish")
            value = require_aware(cutoff) if cutoff is not None else self._now()
            if value > self._now():
                raise QuantSystemError("knowledge cutoff cannot be in the future")
            if any(reference.available_at > value for reference in state.manifest.references):
                raise QuantSystemError("knowledge cutoff predates acquired evidence")
            state.cutoff = value
            for candidate in list(self._runs.values()):
                if candidate.root_run_id == run.root_run_id:
                    self._persist(candidate.run_id)
            self._event(run.root_run_id, "knowledge_cutoff_frozen", knowledge_cutoff=value.isoformat())
            return value

    def _old_request(self, request: RunRequest, digest: str) -> ResearchRun | None:
        previous = self._idempotency.get(request.idempotency_key or "")
        if previous is None:
            return None
        if previous[1] != digest:
            raise ResearchConflict("idempotency key was already used for a different request")
        return self.get(previous[0])

    def _plan_history(self, plan: ResearchPlan | None) -> tuple[str | None, list[CallReceipt]]:
        if plan is None:
            return None, []
        try:
            stored = self.repository.get_plan(plan.plan_id)
        except ArtifactNotFoundError:
            if plan.source == "MODEL":
                raise QuantSystemError("model research plan must be registered before execution") from None
            return None, []
        if hash_json(plan.model_dump(mode="json")) != hash_json(stored.model_dump(mode="json")):
            raise QuantSystemError("research plan does not match the registered plan")
        receipts = self.repository.plan_receipts(stored.plan_id)
        if plan.source == "MODEL" and not receipts:
            raise QuantSystemError("model research plan requires archived planner receipts")
        return stored.plan_id, receipts

    def submit(self, request: RunRequest) -> ResearchRun:
        request = RunRequest.model_validate(request.model_dump()).model_copy(deep=True)
        digest = request_hash(request)
        with self._lock:
            old = self._old_request(request, digest)
            if old is not None:
                return old
            if self._active_root is not None:
                raise ResearchConflict("another root research run is active")
        planning_plan_id, planning_receipts = self._plan_history(request.plan)
        plan = single_task_plan(request, self.module_registry)
        if plan.knowledge_cutoff is not None and plan.knowledge_cutoff > self._now():
            raise QuantSystemError("knowledge cutoff cannot be in the future")
        if plan.knowledge_cutoff is not None and any(
            self.module_registry.get(node.task_type).descriptor.acquisition for node in plan.tasks
        ):
            raise QuantSystemError("acquisition must precede the frozen knowledge cutoff")
        for node in plan.tasks:
            for route in self.module_registry.get(node.task_type).descriptor.roles.values():
                provider, _ = self.config.route(route)
                if self.credentials_required:
                    provider.api_key()
        ceiling = BudgetLimits(
            max_calls=self.config.research_budget.max_llm_calls_per_run,
            max_tokens=self.config.research_budget.max_total_tokens_per_run,
        )
        limits = _limits(request.budget, plan.budget, ceiling)
        composite = request.plan is not None or len(plan.tasks) > 1
        descriptor = (
            self.module_registry.get(request.task_type).descriptor
            if request.task_type and not composite
            else None
        )
        if descriptor is not None:
            limits = _limits(limits, descriptor.default_budget)
        now = self._now()
        run_id = new_id("rrun")
        run = ResearchRun(
            run_id=run_id,
            root_run_id=run_id,
            task_type=descriptor.task_type if descriptor else "runtime.plan",
            module_id=descriptor.module_id if descriptor else "runtime",
            task_version=descriptor.version if descriptor else "1.0",
            result_schema_version=descriptor.result_schema_version
            if descriptor
            else "research-plan-result-v1",
            request=request,
            execution_plan=plan if composite else None,
            created_at=now,
            updated_at=now,
            knowledge_cutoff=plan.knowledge_cutoff,
            usage={"planning_plan_id": planning_plan_id} if planning_plan_id is not None else {},
        )
        with self._lock:
            old = self._old_request(request, digest)
            if old is not None:
                return old
            if self._active_root is not None:
                raise ResearchConflict("another root research run is active")
            if planning_receipts:
                now = self._now()
                try:
                    created = [require_aware(receipt.created_at) for receipt in planning_receipts]
                except ValueError:
                    raise QuantSystemError("planner receipts require aware creation timestamps") from None
                if any(value > now for value in created):
                    raise QuantSystemError("planner receipt creation cannot be in the future")
                remaining = limits.timeout_seconds - (now - min(created)).total_seconds()
                if remaining <= 0:
                    raise QuantSystemError("research planning deadline exceeded")
                limits = limits.model_copy(update={"timeout_seconds": remaining})
            ledger = BudgetLedger(limits, clock=lambda: self._now().timestamp())
            ledger.consume_history(planning_receipts)
            self._states[run_id] = _RootState(ledger, self._manifest(), plan.knowledge_cutoff)
            self._ledgers[run_id] = ledger
            self._manifests[run_id] = self._states[run_id].manifest
            self._runs[run_id] = run
            try:
                saved = self._persist(run_id)
            except BaseException:
                self._runs.pop(run_id, None)
                self._states.pop(run_id, None)
                self._ledgers.pop(run_id, None)
                self._manifests.pop(run_id, None)
                raise
            self._active_root = run_id
            if request.idempotency_key:
                self._idempotency[request.idempotency_key] = (run_id, digest)
            self._event(run_id, "submitted", request_hash=digest)
            return saved

    def _caller(self, run_id: str, task: ResearchTask) -> StructuredCaller:
        roles = {logical: self.config.route(route)[1] for logical, route in task.descriptor.roles.items()}

        def sink(receipt: CallReceipt) -> None:
            receipt = receipt.model_copy(
                update={
                    "call_id": f"{run_id}:{receipt.call_id}",
                    "repair_of": f"{run_id}:{receipt.repair_of}" if receipt.repair_of else None,
                },
                deep=True,
            )
            with self._lock:
                self.repository.call(run_id, receipt)
                self._persist(run_id)
                root_id = self._runs[run_id].root_run_id
                if root_id != run_id:
                    self._persist(root_id)
                self._event(
                    run_id,
                    f"call_{receipt.status.lower()}",
                    call_id=receipt.call_id,
                    sequence=receipt.sequence,
                    role=receipt.role,
                    status=receipt.status,
                )

        return StructuredCaller(
            self.config,
            self.gateway,
            self._ledgers[run_id],
            sink=sink,
            allowed_roles=roles,
        )

    def _invoke(self, run_id: str, task: ResearchTask, payload: dict[str, Any]) -> TaskResult:
        request = validate_request(task, payload)
        context = RunContext(self, run_id, task, self._ledgers[run_id], self._caller(run_id, task))
        context.check()
        if not task.descriptor.acquisition and context.knowledge_cutoff is None:
            context.freeze_cutoff()
        context.advance(
            "executing",
            request_hash=hash_json(request.model_dump(mode="json")),
            resolved_inputs=request.model_dump(mode="json"),
        )
        returned = task.execute(context, request.model_copy(deep=True))
        with self._lock:
            self._event(
                run_id,
                "task_result_received",
                result=returned.model_dump(mode="json") if isinstance(returned, BaseModel) else returned,
            )
        context.check()
        result = TaskResult.model_validate(
            returned.model_dump() if isinstance(returned, BaseModel) else returned
        )
        for name, output_type in (task.output_fields or {}).items():
            if name not in result.payload:
                raise QuantSystemError(f"task omitted its declared output: {name}")
            validate_output(result.payload[name], output_type)
        for reference in result.evidence:
            context.evidence(reference)
        publication = result.publication
        if (
            publication.content_hash is not None
            and re.fullmatch(r"[a-f0-9]{64}", publication.content_hash) is None
        ):
            raise QuantSystemError("publication requires a valid SHA-256 hash")
        if publication.decision == "PASS" and not result.report_artifact_id:
            raise QuantSystemError("publication requires explicit report authorization")
        if result.report_artifact_id:
            report = self.repository.registry.get(result.report_artifact_id)
            verify_artifact(self.repository.store, report)
            approved = report.metadata.get(
                "approved_publication_hash", report.metadata.get("publication_hash")
            )
            if publication.decision == "PASS":
                if report.status.value in {"FAILED", "REJECTED"} or not (
                    report.artifact_type.value == "RESEARCH_REPORT"
                    or report.artifact_type.value.endswith("_RESEARCH_REPORT")
                ):
                    raise QuantSystemError("publication authorization requires an eligible research report")
                if approved is None:
                    raise QuantSystemError("publication requires explicit report authorization")
                if approved != publication.content_hash or any(
                    report.metadata[name] != publication.content_hash
                    for name in ("approved_publication_hash", "publication_hash")
                    if name in report.metadata
                ):
                    raise QuantSystemError("publication hash does not match the report authorization")
        context.check()
        return result.model_copy(deep=True)

    def _finish(
        self,
        run_id: str,
        *,
        result: TaskResult | None = None,
        error: BaseException | None = None,
        retain_summary: bool = False,
    ) -> ResearchRun:
        with self._lock:
            run = self._runs[run_id]
            cancelled = run.cancel_requested or self._runs[run.root_run_id].cancel_requested
            if not cancelled and error is None:
                try:
                    self._ledgers[run_id].check()
                except QuantSystemError as exc:
                    error = exc
            status = "CANCELLED" if cancelled else "FAILED" if error is not None else "COMPLETED"
            saved = self._persist(
                run_id,
                status=status,
                stage=status.lower(),
                result=result if not cancelled or retain_summary else None,
                error=self._error(error)
                if error is not None
                else "research cancelled"
                if cancelled
                else None,
            )
            self._event(run_id, status.lower(), error=saved.error)
            return saved

    def execute(self, run_id: str) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ArtifactNotFoundError(run_id)
            if run.parent_run_id is not None:
                raise QuantSystemError("child tasks are dispatched only by their root plan")
            if run.status != "QUEUED" or run_id in self._executing:
                return
            self._executing.add(run_id)
        result: TaskResult | None = None
        error: BaseException | None = None
        plan = run.execution_plan or run.request.plan
        try:
            with self._lock:
                self._persist(run_id, status="RUNNING", stage="running")
                self._event(run_id, "started")
            self._ledgers[run_id].check()
            if plan is not None:
                result, error = self._execute_plan(run_id, plan)
            else:
                task = self.module_registry.get(run.task_type)
                result = self._invoke(run_id, task, run.request.payload)
                self._freeze(run_id)
        except BaseException as exc:
            error = exc
        finally:
            try:
                if plan is not None:
                    with self._lock:
                        for child_id in self._runs[run_id].children:
                            if self._runs[child_id].status in _ACTIVE:
                                self._finish(
                                    child_id,
                                    error=error or QuantSystemError("root stopped before task dispatch"),
                                )
                        result, child_error = self._plan_result(run_id, plan)
                        error = error or child_error
                self._finish(run_id, result=result, error=error, retain_summary=plan is not None)
            finally:
                with self._lock:
                    self._executing.discard(run_id)
                    if self._active_root == run_id and self._runs[run_id].status not in _ACTIVE:
                        self._active_root = None
        if error is not None and not isinstance(error, Exception):
            raise error

    def _child(self, root_id: str, node: PlanTask, plan: ResearchPlan) -> str:
        task = self.module_registry.get(node.task_type)
        state = self._states[root_id]
        limits = _limits(node.budget, task.descriptor.default_budget, state.ledger.limits)
        child_id = new_id("rrun")
        now = self._now()
        request = RunRequest(
            task_type=node.task_type,
            payload=node.payload,
            budget=limits,
            mode=plan.mode,
            knowledge_cutoff=state.cutoff,
        )
        child = ResearchRun(
            run_id=child_id,
            root_run_id=root_id,
            parent_run_id=root_id,
            task_id=node.task_id,
            task_type=node.task_type,
            module_id=task.descriptor.module_id,
            task_version=task.descriptor.version,
            result_schema_version=task.descriptor.result_schema_version,
            created_at=now,
            updated_at=now,
            request=request,
            knowledge_cutoff=state.cutoff,
        )
        self._runs[child_id] = child
        self._ledgers[child_id] = state.ledger.child(limits)
        self._manifests[child_id] = self._manifest()
        self._persist(child_id)
        if task.descriptor.acquisition:
            state.acquiring.add(child_id)
        return child_id

    def _run_child(self, child_id: str, payload: dict[str, Any]) -> None:
        result = None
        error = None
        try:
            with self._lock:
                self._persist(child_id, status="RUNNING", stage="running")
                self._event(child_id, "started")
            result = self._invoke(child_id, self.module_registry.get(self._runs[child_id].task_type), payload)
        except BaseException as exc:
            error = exc
        finally:
            try:
                self._finish(child_id, result=result, error=error)
            finally:
                with self._lock:
                    self._states[self._runs[child_id].root_run_id].acquiring.discard(child_id)
        if error is not None and not isinstance(error, Exception):
            raise error

    def _execute_plan(self, root_id: str, plan: ResearchPlan) -> tuple[TaskResult, BaseException | None]:
        with self._lock:
            children: dict[str, str] = {}
            for node in plan.tasks:
                children[node.task_id] = self._child(root_id, node, plan)
                self._persist(root_id, children=list(children.values()))
        pending = {node.task_id: node for node in plan.tasks}
        running: dict[Future[None], str] = {}
        done: set[str] = set()
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="research-task") as workers:
            while pending or running:
                self._check(root_id)
                self._states[root_id].ledger.check()
                acquisition_phase = bool(self._states[root_id].acquiring)
                if not acquisition_phase and self._states[root_id].cutoff is None:
                    self._freeze(root_id)
                for task_id, node in list(pending.items()):
                    if len(running) >= 2:
                        break
                    task = self.module_registry.get(node.task_type)
                    if task.descriptor.acquisition != acquisition_phase or not set(node.depends_on) <= done:
                        continue
                    child_id = children[task_id]
                    failed = [
                        parent
                        for parent in node.depends_on
                        if self._runs[children[parent]].status != "COMPLETED"
                    ]
                    if failed:
                        self._finish(
                            child_id, error=QuantSystemError(f"dependency failed: {', '.join(failed)}")
                        )
                        self._states[root_id].acquiring.discard(child_id)
                        done.add(task_id)
                    else:
                        payload = dict(node.payload)
                        for name, reference in node.inputs.items():
                            parent = self._runs[children[reference.task_id]].result
                            if parent is None or reference.output_key not in parent.payload:
                                raise QuantSystemError("declared task output is unavailable")
                            payload[name] = parent.payload[reference.output_key]
                        self._check(root_id)
                        self._states[root_id].ledger.check()
                        running[workers.submit(self._run_child, child_id, deepcopy(payload))] = task_id
                    del pending[task_id]
                if running:
                    completed, _ = wait(running, return_when=FIRST_COMPLETED)
                    for future in completed:
                        task_id = running.pop(future)
                        future.result()
                        done.add(task_id)
                elif pending and acquisition_phase == bool(self._states[root_id].acquiring):
                    raise QuantSystemError("research plan has no runnable tasks")
        return self._plan_result(root_id, plan)

    def _plan_result(self, root_id: str, plan: ResearchPlan) -> tuple[TaskResult, BaseException | None]:
        children = {self._runs[child_id].task_id: child_id for child_id in self._runs[root_id].children}
        required_failed = any(
            node.required
            and (node.task_id not in children or self._runs[children[node.task_id]].status != "COMPLETED")
            for node in plan.tasks
        )
        optional_failed = any(
            not node.required
            and (node.task_id not in children or self._runs[children[node.task_id]].status != "COMPLETED")
            for node in plan.tasks
        )
        summaries = []
        approved = []
        reports = []
        for node in plan.tasks:
            if node.task_id not in children:
                summaries.append(
                    {
                        "task_id": node.task_id,
                        "task_type": node.task_type,
                        "status": "FAILED",
                        "outcome": "NOT_DISPATCHED",
                        "summary": "",
                        "approved_publication_hash": None,
                    }
                )
                continue
            child = self._runs[children[node.task_id]]
            output = child.result
            approved_hash = (
                output.publication.content_hash
                if (
                    output
                    and child.status == "COMPLETED"
                    and output.publication.decision == "PASS"
                    and not self._runs[root_id].cancel_requested
                )
                else None
            )
            summaries.append(
                {
                    "task_id": node.task_id,
                    "run_id": child.run_id,
                    "task_type": child.task_type,
                    "status": child.status,
                    "outcome": output.outcome if output else "TECHNICAL_FAILURE",
                    "summary": output.summary if output else "",
                    "error": child.error,
                    "report_artifact_id": output.report_artifact_id if output else None,
                    "approved_publication_hash": approved_hash,
                }
            )
            if approved_hash:
                approved.append(approved_hash)
            if output and output.report_artifact_id:
                reports.append(output.report_artifact_id)
        result = TaskResult(
            outcome="FAILED" if required_failed else "PARTIAL" if optional_failed else "COMPLETED",
            summary=f"{sum(self._runs[child_id].status == 'COMPLETED' for child_id in children.values())}/{len(children)} tasks completed.",
            payload={
                "plan_id": plan.plan_id,
                "summaries": summaries,
                "report_artifact_ids": reports,
                "approved_publication_hashes": sorted(set(approved)),
            },
            evidence=self._states[root_id].manifest.references,
            limitations=[
                "Parent aggregation references module results and grants no additional publication permission."
            ],
        )
        return result, QuantSystemError("required research task failed") if required_failed else None

    def get(self, run_id: str) -> ResearchRun:
        with self._lock:
            if run_id not in self._runs:
                raise ArtifactNotFoundError(run_id)
            return self.repository.get(run_id).model_copy(deep=True)

    def list_runs(self, offset: int = 0, limit: int = 20) -> list[ResearchRun]:
        self.repository._page(offset, limit)
        with self._lock:
            runs = sorted(
                (run for run in self._runs.values() if run.parent_run_id is None),
                key=lambda run: (run.created_at, run.run_id),
                reverse=True,
            )
            return [self.get(run.run_id) for run in runs[offset : offset + limit]]

    def events(self, run_id: str, offset: int = 0, limit: int = 100) -> list[ResearchEvent]:
        self.get(run_id)
        return self.repository.events(run_id, offset, limit, include_children=True)

    def calls(self, run_id: str, offset: int = 0, limit: int = 100) -> list[CallReceipt]:
        self.repository._page(offset, limit)
        run = self.get(run_id)
        receipts = self.repository.calls(run_id, limit=1_000_000, include_children=True)
        if run.parent_run_id is None and (
            run.usage.get("planning_plan_id") is not None
            or run.request.plan is not None
            and run.request.plan.source == "MODEL"
        ):
            _, history = self._plan_history(run.request.plan)
            receipts = history + receipts
        receipts.sort(key=lambda receipt: (receipt.sequence, receipt.call_id))
        if len({receipt.sequence for receipt in receipts}) != len(receipts):
            raise QuantSystemError("conflicting research call history sequence")
        return [receipt.model_copy(deep=True) for receipt in receipts[offset : offset + limit]]

    def cancel(self, run_id: str) -> ResearchRun:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ArtifactNotFoundError(run_id)
            root_id = run.root_run_id
            root = self._runs[root_id]
            if root.status not in _ACTIVE:
                return self.get(run_id)
            self._states[root_id].ledger.cancel()
            for candidate in list(self._runs.values()):
                if candidate.root_run_id == root_id and candidate.status in _ACTIVE:
                    self._persist(candidate.run_id, cancel_requested=True)
            self._event(run_id, "cancel_requested")
            if root_id not in self._executing:
                self._finish(root_id)
                if self._active_root == root_id:
                    self._active_root = None
            return self.get(run_id)

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = self._runs.get(self._active_root or "")
            modules = []
            for descriptor in self.module_registry.describe():
                missing = []
                for route in descriptor.roles.values():
                    try:
                        provider, _ = self.config.route(route)
                        if self.credentials_required:
                            provider.api_key()
                    except QuantSystemError:
                        missing.append(route)
                modules.append(
                    {
                        **descriptor.model_dump(mode="json"),
                        "ready": descriptor.ready and not missing,
                        "missing_roles": sorted(set(missing)),
                    }
                )
            return {
                "ready": any(module["ready"] for module in modules),
                "active_run_id": self._active_root,
                "busy": self._active_root is not None,
                "cancel_requested": bool(active and active.cancel_requested),
                "usage": deepcopy(active.usage) if active else {},
                "modules": modules,
                "max_parallel_tasks": 2,
                "max_parallel_calls": 3,
            }

    def recover(self) -> None:
        with self._lock:
            if self._executing:
                raise ResearchConflict("cannot recover while a root run is executing")
            self.repository.recover(interrupt=False)
            self._runs = {run.run_id: run for run in self.repository.load()}
            self._states.clear()
            self._ledgers.clear()
            self._manifests.clear()
            self._idempotency.clear()
            seen: set[str] = set()
            for run in sorted(self._runs.values(), key=lambda item: item.parent_run_id is None):
                for receipt in self.repository.calls(run.run_id, limit=1_000_000):
                    if receipt.call_id in seen:
                        continue
                    seen.add(receipt.call_id)
                    if receipt.status == "STARTED":
                        self.repository.call(
                            run.run_id,
                            receipt.model_copy(
                                update={
                                    "status": "UNKNOWN",
                                    "error": "process interrupted; provider outcome is unknown; no replay",
                                    "used_tokens": max(receipt.used_tokens, receipt.reservation_tokens),
                                }
                            ),
                        )
            for run in list(self._runs.values()):
                if run.request.idempotency_key and run.parent_run_id is None:
                    key = run.request.idempotency_key
                    value = (run.run_id, request_hash(run.request))
                    if key in self._idempotency and self._idempotency[key] != value:
                        raise ResearchConflict("persisted idempotency conflict")
                    self._idempotency[key] = value
                if run.status in _ACTIVE:
                    receipts = self.calls(run.run_id, limit=1_000_000)
                    usage = {
                        **run.usage,
                        "calls_used": max(run.usage.get("calls_used", 0), len(receipts)),
                        "tokens_used": max(
                            run.usage.get("tokens_used", 0),
                            sum(
                                max(item.used_tokens, item.reservation_tokens)
                                if item.status in {"STARTED", "UNKNOWN"}
                                else item.used_tokens
                                for item in receipts
                            ),
                        ),
                        "tokens_reserved": 0,
                    }
                    self._persist(
                        run.run_id,
                        status="INTERRUPTED",
                        stage="interrupted",
                        usage=usage,
                        error="process interrupted; unfinished research is not replayed",
                    )
                    self._event(run.run_id, "interrupted")
            self._active_root = None
