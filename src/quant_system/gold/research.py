"""Legacy gold research facade over the shared research runtime.

Schemas live in ``gold.schemas``, fact builders in ``gold.evidence``, the LLM
workflow in ``gold.workflow`` and prompt text in ``gold.prompts``. This module
keeps the dashboard-facing ``GoldResearchService`` projection plus read-only
recovery of pre-runtime ``GOLD_RESEARCH_RUN`` archives, and re-exports the
public names for existing importers.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, get_args

from quant_system.agents.config import AgentLLMConfig
from quant_system.agents.llm import CompletionGateway
from quant_system.artifacts.envelope import ArtifactEnvelope, hash_bytes
from quant_system.domain.enums import ArtifactStatus, ArtifactType
from quant_system.domain.exceptions import ArtifactNotFoundError, QuantSystemError
from quant_system.domain.types import utc_now
from quant_system.gold.evidence import GoldMacroEvidence, MacroEvidenceReader
from quant_system.gold.ingestion import fetch_gold
from quant_system.gold.schemas import (
    ROLES,
    CriticReview,
    GoldAdviceAuditReview,
    GoldInvestmentAdvice,
    GoldOrchestratorDecision,
    GoldResearchBusy,
    GoldResearchRequest,
    GoldResearchRun,
    GoldStrategyProposal,
    GoldTaskRequest,
    RunStage,
    RunStatus,
    StatisticalReview,
)
from quant_system.gold.service import GoldFetcher, GoldService
from quant_system.gold.workflow import GoldWorkflow
from quant_system.research.runtime.calls import BudgetLimits
from quant_system.research.runtime.engine import ResearchConflict, ResearchRuntime
from quant_system.research.runtime.models import (
    InputRef,
    PlanTask,
    ResearchPlan,
    ResearchRun,
    RunRequest,
)
from quant_system.research.runtime.ports import ModuleRegistry, ResearchTask
from quant_system.research.runtime.storage import ArtifactRunRepository

__all__ = [
    "ROLES",
    "CriticReview",
    "GoldAdviceAuditReview",
    "GoldInvestmentAdvice",
    "GoldMacroEvidence",
    "GoldOrchestratorDecision",
    "GoldResearchBusy",
    "GoldResearchRequest",
    "GoldResearchRun",
    "GoldResearchService",
    "GoldStrategyProposal",
    "GoldTaskRequest",
    "GoldWorkflow",
    "MacroEvidenceReader",
    "RunStage",
    "RunStatus",
    "StatisticalReview",
]


class GoldResearchService:
    def __init__(
        self,
        gold: GoldService,
        config: AgentLLMConfig,
        gateway: CompletionGateway,
        *,
        fetcher: GoldFetcher | None = None,
        clock: Callable[[], datetime] = utc_now,
        credentials_required: bool = True,
        runtime: ResearchRuntime | None = None,
        macro_evidence_reader: MacroEvidenceReader | None = None,
    ) -> None:
        self.gold = gold
        self.config = config
        self.gateway = gateway
        self.fetcher = fetcher or fetch_gold
        self.clock = clock
        self.credentials_required = credentials_required
        self.macro_evidence_reader = macro_evidence_reader
        self.max_calls = min(12, config.research_budget.max_llm_calls_per_run)
        self.max_tokens = min(64000, config.research_budget.max_total_tokens_per_run)
        if runtime is None:
            registry = ModuleRegistry()
            for task in self.tasks():
                registry.register(task)
            runtime = ResearchRuntime(
                registry,
                ArtifactRunRepository(gold.store, gold.registry, clock=clock),
                config,
                gateway,
                clock=clock,
                credentials_required=credentials_required,
            )
        self._runtime = runtime

    def tasks(self) -> list[ResearchTask]:
        from quant_system.research.modules.gold import gold_tasks

        return gold_tasks(
            self.gold,
            self.config,
            fetcher=lambda as_of, now: self.fetcher(as_of, now),
            macro_evidence_reader=self.macro_evidence_reader,
        )

    def bind_runtime(self, runtime: ResearchRuntime) -> None:
        for task in self.tasks():
            runtime.module_registry.get(task.descriptor.task_type)
        if self._runtime is not runtime and self._runtime.status()["busy"]:
            raise GoldResearchBusy("已有研究任务进行中，不能替换运行时。")
        self._runtime = runtime

    @property
    def runtime(self) -> ResearchRuntime:
        return self._runtime

    def status(self) -> dict[str, Any]:
        runtime_status = self.runtime.status()
        active_id = runtime_status["active_run_id"]
        active_gold_id = None
        if active_id is not None:
            active = self.runtime.get(active_id)
            plan = active.execution_plan or active.request.plan
            if (
                active.task_type == "gold.research"
                or plan
                and any(node.task_type == "gold.research" for node in plan.tasks)
            ):
                active_gold_id = active_id
        task_status = next(task for task in runtime_status["modules"] if task["task_type"] == "gold.research")
        providers = self.config.credential_status()
        routes = task_status["roles"]
        configured = all(role in routes and routes[role] in self.config.roles for role in ROLES)
        independent = (
            configured
            and self.config.roles[routes["final_auditor"]].model
            != self.config.roles[routes["orchestrator"]].model
        )
        credentials = configured and all(
            providers[self.config.roles[routes[role]].provider] for role in ROLES
        )
        return {
            "ready": task_status["ready"] and independent and (credentials or not self.credentials_required),
            "roles": {
                role: {"provider": self.config.roles[route].provider, "model": self.config.roles[route].model}
                for role, route in routes.items()
                if route in self.config.roles
            },
            "protocol": "gold-deliberation-v4",
            "budget": {
                "max_calls": min(self.max_calls, task_status["default_budget"]["max_calls"]),
                "max_tokens": min(self.max_tokens, task_status["default_budget"]["max_tokens"]),
            },
            "active_run_id": runtime_status["active_run_id"],
            "active_gold_run_id": active_gold_id,
            "busy": runtime_status["busy"],
        }

    @staticmethod
    def _request(run: ResearchRun) -> GoldResearchRequest:
        plan = run.execution_plan or run.request.plan
        if plan is not None:
            node = next((node for node in plan.tasks if node.task_type == "gold.research"), None)
            if node is None:
                raise ArtifactNotFoundError(run.run_id)
            payload = node.payload
        elif run.task_type == "gold.research":
            payload = run.request.payload
        else:
            raise ArtifactNotFoundError(run.run_id)
        return GoldResearchRequest.model_validate(
            {key: value for key, value in payload.items() if key in GoldResearchRequest.model_fields}
        )

    def _project(self, run: ResearchRun) -> GoldResearchRun:
        request = self._request(run)
        children = [self.runtime.get(child_id) for child_id in run.children]
        research = next(
            (child for child in children if child.task_type == "gold.research"),
            run if run.task_type == "gold.research" else None,
        )
        collect = next((child for child in children if child.task_type == "gold.collect"), None)
        result = (
            research.result
            if research and research.status == "COMPLETED" and run.status not in {"CANCELLED", "INTERRUPTED"}
            else None
        )
        snapshot_id = result.payload.get("analysis_context", {}).get("snapshot_id") if result else None
        if snapshot_id is None and collect and collect.result:
            snapshot_id = collect.result.payload.get("snapshot_id")
        if snapshot_id is None and research:
            snapshot_id = research.request.payload.get("snapshot_id")
        if snapshot_id is None:
            snapshot_id = next(
                (
                    event.data["snapshot_id"]
                    for event in self.runtime.events(run.run_id, limit=1_000_000)
                    if "snapshot_id" in event.data
                ),
                None,
            )
        active = next((child for child in children if child.status == "RUNNING"), research or collect or run)
        failed = next((child for child in children if child.status == "FAILED"), None)
        interrupted = run.status in {"INTERRUPTED", "CANCELLED"}
        status = "FAILED" if interrupted else run.status
        stage = (
            "complete"
            if status == "COMPLETED"
            else "failed"
            if status == "FAILED"
            else "queued"
            if status == "QUEUED"
            else active.stage
        )
        if stage not in get_args(RunStage):
            stage = "fetch" if active.task_type == "gold.collect" else "evaluation"
        error = (
            "API进程已重启或任务已取消；中断任务不会自动重试或重复计费。"
            if interrupted
            else failed.error
            if failed
            else run.error
        )
        return GoldResearchRun.model_validate(
            {
                "run_id": run.run_id,
                "protocol": "gold-deliberation-v4",
                "status": status,
                "stage": stage,
                "created_at": run.created_at,
                "updated_at": run.updated_at,
                "request": request,
                "snapshot_id": snapshot_id,
                "report_artifact_id": result.report_artifact_id if result else None,
                "result": result.payload if result else None,
                "error": error,
            }
        )

    def _legacy(self, *, recover: bool = False) -> dict[str, GoldResearchRun]:
        latest: dict[str, ArtifactEnvelope] = {}
        for artifact in self.gold.registry.find_by_type(ArtifactType.GOLD_RESEARCH_RUN):
            run_id = str(artifact.metadata["run_id"])
            previous = latest.get(run_id)
            if previous is None or int(artifact.metadata["revision"]) > int(previous.metadata["revision"]):
                latest[run_id] = artifact
        runs = {}
        for run_id, artifact in latest.items():
            raw = self.gold.store.get_bytes(artifact.uri)
            if hash_bytes(raw) != artifact.content_hash:
                raise ValueError("gold research artifact content hash mismatch")
            payload = json.loads(raw)
            run = GoldResearchRun.model_validate(payload["run"])
            if run.run_id != run_id:
                raise ValueError("gold research run identity mismatch")
            if run.status in {"QUEUED", "RUNNING"}:
                run = run.model_copy(
                    update={
                        "status": "FAILED",
                        "stage": "failed",
                        "error": "API进程已重启；中断任务不会自动重试或重复计费。",
                    }
                )
                if recover:
                    revision = int(artifact.metadata["revision"]) + 1
                    uri, digest = self.gold.store.put_content_addressed_json(
                        "gold/research/legacy-recovery",
                        {
                            **payload,
                            "run": run.model_dump(mode="json"),
                            "revision": revision,
                        },
                    )
                    self.gold.registry.register(
                        ArtifactEnvelope(
                            artifact_type=ArtifactType.GOLD_RESEARCH_RUN,
                            producer="gold-legacy-recovery",
                            created_at=self.clock(),
                            uri=uri,
                            content_hash=digest,
                            parent_artifact_ids=(artifact.artifact_id,),
                            data_snapshot_id=artifact.data_snapshot_id,
                            status=ArtifactStatus.FAILED,
                            metadata={
                                "run_id": run_id,
                                "revision": revision,
                                "status": "FAILED",
                                "stage": "failed",
                            },
                        )
                    )
            runs[run_id] = run
        return runs

    def recover_legacy(self) -> None:
        self._legacy(recover=True)

    def get(self, run_id: str) -> GoldResearchRun:
        try:
            return self._project(self.runtime.get(run_id))
        except ArtifactNotFoundError:
            legacy = self._legacy()
            if run_id not in legacy:
                raise ArtifactNotFoundError(run_id) from None
            return legacy[run_id].model_copy(deep=True)

    def list_runs(self) -> list[GoldResearchRun]:
        runs = self._legacy()
        for run in self.runtime.list_runs(limit=1_000_000):
            plan = run.execution_plan or run.request.plan
            if (
                run.task_type == "gold.research"
                or plan is not None
                and any(node.task_type == "gold.research" for node in plan.tasks)
            ):
                runs[run.run_id] = self._project(run)
        return sorted(runs.values(), key=lambda run: (run.created_at, run.run_id), reverse=True)[:20]

    def create(self, request: GoldResearchRequest) -> GoldResearchRun:
        if not self.status()["ready"]:
            raise QuantSystemError("研究角色或独立终审路由/凭据未就绪；终审模型必须不同于统筹模型。")
        budget = BudgetLimits(max_calls=self.max_calls, max_tokens=self.max_tokens)
        plan = ResearchPlan(
            question=request.focus,
            budget=budget,
            tasks=[
                PlanTask(
                    task_id="collect",
                    task_type="gold.collect",
                    payload=request.model_dump(mode="json"),
                    budget=budget,
                ),
                PlanTask(
                    task_id="research",
                    task_type="gold.research",
                    payload=request.model_dump(mode="json"),
                    inputs={"snapshot_id": InputRef(task_id="collect", output_key="snapshot_id")},
                    depends_on=["collect"],
                    budget=budget,
                ),
            ],
        )
        try:
            run = self.runtime.submit(RunRequest(plan=plan, budget=budget))
        except ResearchConflict as exc:
            raise GoldResearchBusy(str(exc)) from exc
        return self._project(run)

    def execute(self, run_id: str) -> None:
        if run_id in self._legacy():
            return
        self.runtime.execute(run_id)
