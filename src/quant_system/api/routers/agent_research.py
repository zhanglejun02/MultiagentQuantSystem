"""Legacy multi-agent factor research routes backed by the research runtime."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from quant_system.agents.orchestration import AgentResearchRequest
from quant_system.api.common import env_to_dict
from quant_system.api.container import ApiContainer
from quant_system.domain.enums import ArtifactType
from quant_system.domain.exceptions import QuantSystemError
from quant_system.research.runtime.engine import ResearchConflict
from quant_system.research.runtime.models import RunRequest


def build_router(c: ApiContainer) -> APIRouter:
    router = APIRouter(prefix="/api/agent-research", tags=["agent-research"])

    @router.get("/status")
    def agent_research_status() -> dict:
        factor_status = next(
            module
            for module in c.research_runtime.status()["modules"]
            if module["task_type"] == "factor.research"
        )
        return {
            "ready": factor_status["ready"],
            "providers": c.agent_config.credential_status(),
            "roles": {
                role: {"provider": route.provider, "model": route.model}
                for role, route in c.agent_config.roles.items()
                if role in {"researcher", "critic", "statistician", "orchestrator"}
            },
            "budget": c.agent_config.research_budget.model_dump(),
            "factor_validation": c.agent_config.factor_validation.model_dump(),
        }

    @router.get("/runs")
    def list_agent_research_runs() -> list[dict]:
        reports = c.registry.find_by_type(ArtifactType.RESEARCH_REPORT)
        return sorted(
            (
                env_to_dict(report)
                for report in reports
                if report.metadata.get("module_id", "factor") == "factor"
            ),
            key=lambda item: item["created_at"],
            reverse=True,
        )

    @router.post("/runs", status_code=201)
    def run_agent_research(body: AgentResearchRequest) -> dict:
        try:
            run = c.research_runtime.submit(
                RunRequest(
                    task_type="factor.research",
                    payload={
                        **body.model_dump(mode="json"),
                        "data_mode": "frozen_snapshot" if body.data_snapshot_id else "fixture",
                    },
                )
            )
            c.research_runtime.execute(run.run_id)
            completed = c.research_runtime.get(run.run_id)
            if completed.status != "COMPLETED" or completed.result is None:
                raise QuantSystemError(completed.error or "factor research did not complete")
        except ResearchConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except QuantSystemError as exc:
            raise HTTPException(422, str(exc)) from exc
        return completed.result.payload

    return router
