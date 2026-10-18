"""Factor task registration over the legacy research facade and shared runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from quant_system.agents.orchestration import (
    AgentResearchOrchestrator,
    AgentResearchRequest,
)
from quant_system.research.runtime.calls import BudgetLimits
from quant_system.research.runtime.engine import RunContext
from quant_system.research.runtime.models import Publication, TaskDescriptor, TaskResult
from quant_system.research.runtime.ports import ResearchTask


class FactorTaskRequest(AgentResearchRequest):
    model_config = ConfigDict(extra="forbid", frozen=True)

    data_mode: Literal["fixture", "frozen_snapshot"] = "fixture"

    @model_validator(mode="after")
    def validate_scope(self) -> FactorTaskRequest:
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be before end_date")
        if self.data_mode == "frozen_snapshot" and not self.data_snapshot_id:
            raise ValueError("frozen_snapshot research requires data_snapshot_id")
        if self.data_mode == "fixture" and self.data_snapshot_id is not None:
            raise ValueError("data_snapshot_id requires frozen_snapshot data_mode")
        return self


def factor_tasks(
    service_factory: Callable[[FactorTaskRequest], AgentResearchOrchestrator] | AgentResearchOrchestrator,
) -> list[ResearchTask]:
    def execute(context: RunContext, request: BaseModel) -> TaskResult:
        validated = FactorTaskRequest.model_validate(request.model_dump())
        service = (
            service_factory
            if isinstance(service_factory, AgentResearchOrchestrator)
            else service_factory(validated)
        )
        result = service.run(validated, context=context)
        return TaskResult(
            outcome=result.experiment.state.value,
            summary=result.decision.rationale,
            payload=result.model_dump(mode="json"),
            evidence=[service.snapshot_evidence(validated.data_snapshot_id)]
            if validated.data_snapshot_id is not None
            else [],
            publication=Publication(decision="NOT_APPLICABLE"),
            report_artifact_id=result.report_artifact_id,
        )

    return [
        ResearchTask(
            descriptor=TaskDescriptor(
                module_id="factor",
                task_type="factor.research",
                title="Factor research",
                roles={role: role for role in ("researcher", "critic", "statistician", "orchestrator")},
                default_budget=BudgetLimits(max_calls=30),
                result_schema=TaskResult.model_json_schema(),
            ),
            request_type=FactorTaskRequest,
            execute=execute,
            output_fields={"report_artifact_id": str},
        )
    ]
