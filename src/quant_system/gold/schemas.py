"""Request/response schemas and run projection models for gold research."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quant_system.agents.config import AgentRoleName
from quant_system.agents.deliberation import ANALYSTS
from quant_system.domain.exceptions import QuantSystemError
from quant_system.gold.analysis import StrategyParameters

ROLES: tuple[AgentRoleName, ...] = (*ANALYSTS, "orchestrator", "final_auditor")
RunStatus = Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED"]
RunStage = Literal[
    "queued",
    "fetch",
    "researcher",
    "critic",
    "evaluation",
    "statistician",
    "orchestrator",
    "independent_analysis",
    "cross_review",
    "analyst_revision",
    "final_audit",
    "advice_revision",
    "final_reaudit",
    "complete",
    "failed",
]
AdviceEvidenceKey = Literal[
    "gold_return_126",
    "gold_return_252",
    "gold_ma120_gap",
    "gold_ma250_gap",
    "gold_drawdown_252",
    "diagnostic_excess_return",
    "double_cost_return",
    "independent_oos",
]
AdviceStance = Literal["WAIT", "GRADUAL_ALLOCATION", "DEFENSIVE"]


class GoldResearchBusy(QuantSystemError):
    pass


class GoldResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)

    symbol: Literal["Au99.99", "518880", "518800", "159934", "159937"] = "Au99.99"
    max_weight: float = Field(default=0.10, ge=0, le=0.30)
    focus: str = Field(
        default="分析人民币黄金趋势与风险，提出可检验策略及3至12个月配置建议", min_length=1, max_length=1000
    )


class GoldTaskRequest(GoldResearchRequest):
    snapshot_id: str = Field(min_length=1, strict=True)
    macro_snapshot_id: str | None = Field(default=None, min_length=1, strict=True)


class GoldStrategyProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    hypothesis: str = Field(min_length=1, max_length=1200)
    market_view: str = Field(min_length=1, max_length=1800)
    risks: list[str] = Field(min_length=1, max_length=10)
    parameters: StrategyParameters


class GoldAdviceReason(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_key: AdviceEvidenceKey
    implication: str = Field(min_length=1, max_length=65)


class GoldInvestmentAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    horizon: Literal["3至12个月"] = "3至12个月"
    stance: AdviceStance
    summary: str = Field(min_length=1, max_length=140)
    reasons: list[GoldAdviceReason] = Field(min_length=2, max_length=3)
    reassess_when: str = Field(min_length=1, max_length=100)


class _GoldSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    rationale: str = Field(min_length=1, max_length=2000)


class CriticReview(_GoldSummary):
    approved: bool
    issues: list[str] = Field(default_factory=list)

    @field_validator("issues", mode="before")
    @classmethod
    def normalize_issues(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value


class StatisticalReview(_GoldSummary):
    robust: bool
    findings: list[str] = Field(default_factory=list)

    @field_validator("findings", mode="before")
    @classmethod
    def normalize_findings(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value


class GoldOrchestratorDecision(_GoldSummary):
    recommend_candidate: bool
    next_steps: list[str] = Field(default_factory=list)
    investment_advice: GoldInvestmentAdvice

    @field_validator("next_steps", mode="before")
    @classmethod
    def normalize_next_steps(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value


class GoldAdviceAuditReview(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)

    decision: Literal["PASS", "REVISE", "REJECT"]
    unsupported_claims: list[str] = Field(max_length=6)
    missing_risks: list[str] = Field(max_length=6)
    confidence_adjustment: float = Field(ge=-1, le=0)
    rationale: str = Field(min_length=1, max_length=600)

    def validate_pass(self) -> None:
        if self.decision == "PASS" and (
            self.unsupported_claims or self.missing_risks or self.confidence_adjustment < 0
        ):
            raise ValueError("PASS cannot contain unresolved issues or a required confidence reduction")


class GoldResearchRun(BaseModel):
    run_id: str
    protocol: str | None = None
    status: RunStatus = "QUEUED"
    stage: RunStage = "queued"
    created_at: datetime
    updated_at: datetime
    request: GoldResearchRequest
    snapshot_id: str | None = None
    report_artifact_id: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
