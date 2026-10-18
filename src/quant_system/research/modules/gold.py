from __future__ import annotations

from pydantic import BaseModel

from quant_system.agents.config import AgentLLMConfig
from quant_system.gold.research import (
    ROLES,
    GoldResearchRequest,
    GoldTaskRequest,
    GoldWorkflow,
    MacroEvidenceReader,
)
from quant_system.gold.service import GoldFetcher, GoldService
from quant_system.research.runtime.calls import BudgetLimits
from quant_system.research.runtime.engine import RunContext
from quant_system.research.runtime.models import TaskDescriptor, TaskResult
from quant_system.research.runtime.ports import ResearchTask


def gold_tasks(
    gold: GoldService,
    config: AgentLLMConfig | None = None,
    *,
    fetcher: GoldFetcher | None = None,
    macro_evidence_reader: MacroEvidenceReader | None = None,
) -> list[ResearchTask]:
    workflow = GoldWorkflow(
        gold,
        fetcher=fetcher,
        macro_evidence_reader=macro_evidence_reader,
        configuration=config.model_dump(mode="json") if config is not None else None,
    )

    def collect(ctx: RunContext, request: BaseModel) -> TaskResult:
        return workflow.collect(ctx, GoldResearchRequest.model_validate(request.model_dump()))

    def research(ctx: RunContext, request: BaseModel) -> TaskResult:
        return workflow.research(ctx, GoldTaskRequest.model_validate(request.model_dump()))

    return [
        ResearchTask(
            TaskDescriptor(
                task_type="gold.collect",
                module_id="gold",
                title="Gold market collection",
                version="shared-runtime-v1",
                acquisition=True,
                default_budget=BudgetLimits(max_calls=0, max_tokens=0),
                result_schema={
                    "type": "object",
                    "required": ["snapshot_id"],
                    "properties": {"snapshot_id": {"type": "string"}},
                },
            ),
            GoldResearchRequest,
            collect,
            output_fields={"snapshot_id": str},
        ),
        ResearchTask(
            TaskDescriptor(
                task_type="gold.research",
                module_id="gold",
                title="Gold evidence deliberation",
                version="shared-runtime-v1",
                result_schema_version="gold-deliberation-v4",
                roles={role: role for role in ROLES},
                default_budget=BudgetLimits(max_calls=12, max_tokens=64000),
            ),
            GoldTaskRequest,
            research,
        ),
    ]
