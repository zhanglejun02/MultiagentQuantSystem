from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant_system.domain.types import new_id, require_aware, utc_now
from quant_system.research.runtime.calls import BudgetLimits


class ResearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class EvidenceRef(ResearchModel):
    artifact_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    schema_version: str = "1.0"
    kind: Literal["FACT", "DERIVED", "OPINION"] = "FACT"
    available_at: datetime
    namespace: str = Field(default="research", pattern=r"^[a-z][a-z0-9_.-]*$")
    record_key: str | None = None

    @field_validator("available_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class Publication(ResearchModel):
    decision: Literal["PASS", "WITHHELD", "NOT_APPLICABLE"] = "NOT_APPLICABLE"
    content_hash: str | None = None
    reason: str = ""

    @model_validator(mode="after")
    def approved_hash(self) -> Publication:
        if self.decision == "PASS" and not self.content_hash:
            raise ValueError("published output requires the approved content hash")
        return self


class TaskResult(ResearchModel):
    outcome: str
    summary: str = Field(max_length=2000)
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    publication: Publication = Field(default_factory=Publication)
    limitations: list[str] = Field(default_factory=list)
    report_artifact_id: str | None = None


class TaskDescriptor(ResearchModel):
    task_type: str = Field(pattern=r"^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_.-]*$")
    module_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    title: str
    version: str = "1.0"
    result_schema_version: str = "1.0"
    roles: dict[str, str] = Field(default_factory=dict)
    tools: dict[str, list[str]] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    default_budget: BudgetLimits = Field(default_factory=BudgetLimits)
    acquisition: bool = False
    ready: bool = True
    unavailable_reason: str | None = None
    request_schema: dict[str, Any] = Field(default_factory=dict)
    result_schema: dict[str, Any] = Field(default_factory=dict)


class InputRef(ResearchModel):
    task_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    output_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class PlanTask(ResearchModel):
    task_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    task_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, InputRef] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    required: bool = True
    budget: BudgetLimits = Field(default_factory=BudgetLimits)


class ResearchPlan(ResearchModel):
    schema_version: Literal["research-plan-v1"] = "research-plan-v1"
    plan_id: str = Field(default_factory=lambda: new_id("rplan"))
    question: str = Field(default="", max_length=1000)
    tasks: list[PlanTask] = Field(min_length=1, max_length=6)
    mode: Literal["LATEST", "HISTORICAL"] = "LATEST"
    knowledge_cutoff: datetime | None = None
    budget: BudgetLimits = Field(default_factory=BudgetLimits)
    source: Literal["EXPLICIT", "RULE", "MODEL"] = "EXPLICIT"

    @field_validator("knowledge_cutoff")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value else None

    @model_validator(mode="after")
    def historical_cutoff(self) -> ResearchPlan:
        if self.mode == "HISTORICAL" and self.knowledge_cutoff is None:
            raise ValueError("historical research requires an explicit knowledge cutoff")
        return self


class RunRequest(ResearchModel):
    task_type: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    plan: ResearchPlan | None = None
    budget: BudgetLimits = Field(default_factory=BudgetLimits)
    knowledge_cutoff: datetime | None = None
    mode: Literal["LATEST", "HISTORICAL"] = "LATEST"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("knowledge_cutoff")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value else None

    @model_validator(mode="after")
    def one_entry(self) -> RunRequest:
        if bool(self.task_type) == bool(self.plan):
            raise ValueError("provide exactly one task_type or plan")
        if self.mode == "HISTORICAL" and not self.knowledge_cutoff and not self.plan:
            raise ValueError("historical research requires a knowledge cutoff")
        return self


RunStatus = Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"]


class ResearchRun(ResearchModel):
    run_id: str = Field(default_factory=lambda: new_id("rrun"))
    root_run_id: str
    parent_run_id: str | None = None
    task_id: str | None = None
    task_type: str
    module_id: str
    task_version: str = "1.0"
    result_schema_version: str = "1.0"
    status: RunStatus = "QUEUED"
    stage: str = "queued"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    request: RunRequest
    execution_plan: ResearchPlan | None = None
    knowledge_cutoff: datetime | None = None
    result: TaskResult | None = None
    error: str | None = None
    children: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    cancel_requested: bool = False
    revision: int = 0


class ResearchEvent(ResearchModel):
    run_id: str
    sequence: int
    event_type: str
    stage: str
    at: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)
