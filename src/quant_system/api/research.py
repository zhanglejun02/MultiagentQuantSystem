from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from quant_system.agents.config import AgentLLMConfig
from quant_system.agents.llm import CompletionGateway
from quant_system.research.runtime.calls import BudgetLimits, CallReceipt
from quant_system.research.runtime.engine import (
    ArtifactNotFoundError,
    QuantSystemError,
    ResearchConflict,
    ResearchRuntime,
    RunContext,
)
from quant_system.research.runtime.models import (
    ResearchEvent,
    ResearchPlan,
    ResearchRun,
    RunRequest,
    TaskDescriptor,
    TaskResult,
)
from quant_system.research.runtime.planning import ResearchPlanner
from quant_system.research.runtime.ports import ResearchTask


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=1000)
    payload: dict[str, Any] = Field(default_factory=dict)
    use_model: bool = False
    budget: BudgetLimits = Field(default_factory=BudgetLimits)


class PlanResponse(BaseModel):
    plan: ResearchPlan
    artifact_id: str
    receipts: list[CallReceipt]


def _reject_child_planning(plan: ResearchPlan) -> None:
    if any(task.task_type == "research.plan" for task in plan.tasks):
        raise QuantSystemError("research.plan is a root-only preview task, not a plan child")


def _message(error: BaseException | str, config: AgentLLMConfig) -> str:
    message = str(error)
    secrets = {os.environ.get(provider.api_key_env, "").strip() for provider in config.providers.values()}
    for secret in sorted(secrets - {""}, key=len, reverse=True):
        message = message.replace(secret, "[REDACTED]")
    return message[:4000]


def _failure_status(message: str) -> int:
    return 503 if "missing LLM credential" in message else 422


@contextmanager
def _research_errors(config: AgentLLMConfig) -> Generator[None]:
    try:
        yield
    except ResearchConflict as error:
        raise HTTPException(status_code=409, detail=_message(error, config)) from None
    except ArtifactNotFoundError as error:
        raise HTTPException(status_code=404, detail=_message(error, config)) from None
    except ValidationError as error:
        raise HTTPException(
            status_code=422,
            detail=error.errors(include_url=False, include_context=False, include_input=False),
        ) from None
    except QuantSystemError as error:
        message = _message(error, config)
        raise HTTPException(status_code=_failure_status(message), detail=message) from None


def register_research_routes(
    app: FastAPI,
    runtime: ResearchRuntime,
    config: AgentLLMConfig,
    gateway: CompletionGateway,
) -> None:
    def propose_model(context: RunContext, request: BaseModel) -> TaskResult:
        if context.run_id != context.root_run_id:
            raise QuantSystemError("research.plan is a root-only preview task, not a plan child")
        prepared = PlanRequest.model_validate(request.model_dump())
        context.advance("planning")
        plan = ResearchPlanner(runtime.module_registry, config, gateway).propose(
            prepared.question,
            payload=prepared.payload,
            use_model=True,
            budget=prepared.budget,
            caller=context.caller,
        )
        _reject_child_planning(plan)
        context.check()
        artifact_id = runtime.repository.save_plan(plan, receipts=context.caller.receipts)
        return TaskResult(
            outcome="PROPOSED",
            summary=prepared.question,
            payload={"plan": plan.model_dump(mode="json"), "artifact_id": artifact_id},
        )

    if "research.plan" not in {task.task_type for task in runtime.module_registry.describe()}:
        runtime.module_registry.register(
            ResearchTask(
                descriptor=TaskDescriptor(
                    task_type="research.plan",
                    module_id="research",
                    title="Research planning",
                    roles={
                        "research.planner": (
                            "research.planner" if "research.planner" in config.roles else "orchestrator"
                        )
                    },
                    default_budget=BudgetLimits(max_calls=2, max_tokens=30_000),
                    result_schema=TaskResult.model_json_schema(),
                ),
                request_type=PlanRequest,
                execute=propose_model,
            )
        )

    router = APIRouter(prefix="/api/research", tags=["research"])

    @router.get("/modules")
    def modules() -> dict[str, Any]:
        with _research_errors(config):
            return runtime.status()

    @router.get("/runs")
    def runs(
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=0, le=1000)] = 20,
    ) -> list[ResearchRun]:
        with _research_errors(config):
            return runtime.list_runs(offset=offset, limit=limit)

    @router.post("/runs", status_code=202)
    def submit(request: RunRequest, background_tasks: BackgroundTasks) -> ResearchRun:
        with _research_errors(config):
            if request.plan is not None:
                _reject_child_planning(request.plan)
            run = runtime.submit(request)
            if run.status == "QUEUED":
                background_tasks.add_task(runtime.execute, run.run_id)
            return run

    @router.get("/runs/{run_id}")
    def detail(run_id: str) -> ResearchRun:
        with _research_errors(config):
            return runtime.get(run_id)

    @router.get("/runs/{run_id}/events")
    def events(
        run_id: str,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=0, le=1000)] = 100,
    ) -> list[ResearchEvent]:
        with _research_errors(config):
            return runtime.events(run_id, offset=offset, limit=limit)

    @router.get("/runs/{run_id}/calls")
    def calls(
        run_id: str,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=0, le=1000)] = 100,
    ) -> list[CallReceipt]:
        with _research_errors(config):
            return runtime.calls(run_id, offset=offset, limit=limit)

    @router.post("/runs/{run_id}/cancel")
    def cancel(run_id: str) -> ResearchRun:
        with _research_errors(config):
            return runtime.cancel(run_id)

    @router.post("/plans")
    def plans(request: PlanRequest) -> PlanResponse:
        with _research_errors(config):
            if not request.use_model:
                plan = ResearchPlanner(runtime.module_registry, config, gateway).propose(
                    request.question, payload=request.payload, budget=request.budget
                )
                _reject_child_planning(plan)
                artifact_id = runtime.repository.save_plan(plan)
                return PlanResponse(plan=plan, artifact_id=artifact_id, receipts=[])
            submitted = runtime.submit(
                RunRequest(
                    task_type="research.plan",
                    payload=request.model_dump(mode="json"),
                    budget=request.budget,
                )
            )
            runtime.execute(submitted.run_id)
            run = runtime.get(submitted.run_id)
            if run.status != "COMPLETED" or run.result is None:
                message = _message(run.error or "research planning did not complete", config)
                raise HTTPException(status_code=_failure_status(message), detail=message)
            return PlanResponse.model_validate({**run.result.payload, "receipts": runtime.calls(run.run_id)})

    app.include_router(router)
