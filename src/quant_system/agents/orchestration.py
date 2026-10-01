"""Auditable multi-agent factor research orchestration.

LLMs propose and review structured research. Only the local FactorLab parses
and evaluates factor expressions, and only local gates can promote a run.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quant_system.agents.config import AgentLLMConfig, AgentRoleName
from quant_system.agents.llm import LLMMessage, LLMResponse
from quant_system.artifacts.envelope import hash_json
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.types import research_report_artifact
from quant_system.domain.enums import ExperimentState
from quant_system.domain.exceptions import QuantSystemError
from quant_system.domain.types import new_id
from quant_system.factors.dsl import FactorExpression, FactorExpressionError
from quant_system.factors.evaluation import FactorLab, FactorRun
from quant_system.research.experiments import Experiment, ExperimentRun, ExperimentService


class CompletionGateway(Protocol):
    def complete(self, role: AgentRoleName, messages: list[LLMMessage]) -> LLMResponse: ...


class AgentResearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    topic: str = Field(min_length=3, max_length=500)
    universe_id: str = "cn_a_all"
    start_date: date
    end_date: date
    target: str = "NEXT_OPEN_TO_CLOSE"


class FactorCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=100)
    hypothesis: str = Field(min_length=1, max_length=1000)
    expression: str = Field(min_length=1, max_length=500)
    horizon: int = Field(ge=1, le=20)

    @field_validator("horizon", mode="before")
    @classmethod
    def _normalize_horizon(cls, value: Any) -> Any:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized.upper() in {
                "NEXT_OPEN_TO_CLOSE",
                "NEXT_CLOSE_TO_CLOSE",
                "NEXT_OPEN_TO_NEXT_OPEN",
            }:
                return 1
            if normalized.endswith("d"):
                normalized = normalized[:-1]
            if normalized.isdigit():
                return int(normalized)
        return value


class CriticReview(BaseModel):
    model_config = ConfigDict(frozen=True)

    approved: bool
    issues: list[str] = []
    rationale: str = Field(min_length=1, max_length=2000)

    @field_validator("issues", mode="before")
    @classmethod
    def _normalize_issues(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value


class StatisticalReview(BaseModel):
    model_config = ConfigDict(frozen=True)

    robust: bool
    findings: list[str] = []
    rationale: str = Field(min_length=1, max_length=2000)

    @field_validator("findings", mode="before")
    @classmethod
    def _normalize_findings(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value


class OrchestratorDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    recommend_candidate: bool
    rationale: str = Field(min_length=1, max_length=2000)
    next_steps: list[str] = []

    @field_validator("next_steps", mode="before")
    @classmethod
    def _normalize_next_steps(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value


class AgentCallTrace(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: AgentRoleName
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    raw_request_id: str | None


class AgentResearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    trace_id: str
    experiment: Experiment
    experiment_run: ExperimentRun
    factor_run: FactorRun
    candidate: FactorCandidate
    critic: CriticReview
    statistician: StatisticalReview
    decision: OrchestratorDecision
    local_gates: dict[str, bool]
    report_artifact_id: str
    calls: list[AgentCallTrace]


StructuredModel = TypeVar("StructuredModel", bound=BaseModel)


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _parse_structured(content: str, model: type[StructuredModel]) -> StructuredModel:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            start = 1
            if lines[0].strip().lower() not in {"```", "```json"}:
                raise QuantSystemError(f"{model.__name__} response has an unsupported code fence")
            text = "\n".join(lines[start:-1])
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuantSystemError(f"{model.__name__} response is not valid JSON") from exc
    try:
        return model.model_validate(payload)
    except ValueError as exc:
        raise QuantSystemError(f"{model.__name__} response does not match schema: {exc}") from exc


class AgentResearchOrchestrator:
    def __init__(
        self,
        *,
        config: AgentLLMConfig,
        gateway: CompletionGateway,
        experiments: ExperimentService,
        factor_lab: FactorLab,
        registry: ArtifactRegistry,
        code_commit: str = "unknown",
    ) -> None:
        self._config = config
        self._gateway = gateway
        self._experiments = experiments
        self._factor_lab = factor_lab
        self._registry = registry
        self._code_commit = code_commit

    def run(self, request: AgentResearchRequest) -> AgentResearchResult:
        if request.start_date >= request.end_date:
            raise QuantSystemError("start_date must be before end_date")
        if self._config.research_budget.max_llm_calls_per_run < 4:
            raise QuantSystemError("research budget requires at least four LLM calls")

        trace_id = new_id("atrace_")
        experiment = self._experiments.create(
            request.topic,
            "Four-agent PIT-safe factor research run",
            metadata={"trace_id": trace_id, "request": request.model_dump(mode="json")},
        )
        calls: list[AgentCallTrace] = []
        try:
            candidate = self._call(
                "researcher",
                FactorCandidate,
                self._researcher_messages(request),
                calls,
            )
            try:
                FactorExpression(candidate.expression)
            except FactorExpressionError as exc:
                candidate = self._call(
                    "researcher",
                    FactorCandidate,
                    self._researcher_repair_messages(request, candidate, str(exc)),
                    calls,
                )
                FactorExpression(candidate.expression)
            experiment = self._experiments.transition(
                experiment.experiment_id,
                ExperimentState.PROPOSED,
                reason="researcher produced a structured factor candidate",
                actor="agent:researcher",
            )
            critic = self._call(
                "critic",
                CriticReview,
                self._critic_messages(request, candidate),
                calls,
            )
            experiment = self._experiments.transition(
                experiment.experiment_id,
                ExperimentState.SPEC_READY,
                reason=(
                    "critic approved candidate for deterministic evaluation"
                    if critic.approved
                    else "critic rejected candidate; deterministic evaluation retained for audit"
                ),
                actor="agent:critic",
            )
            experiment = self._experiments.transition(
                experiment.experiment_id,
                ExperimentState.RUNNING,
                reason="local FactorLab evaluation started",
                actor="system:factor_lab",
            )
            factor_run = self._factor_lab.evaluate(
                expression=candidate.expression,
                universe_id=request.universe_id,
                start_date=request.start_date,
                end_date=request.end_date,
                target=request.target,
                horizon=candidate.horizon,
            )
            experiment = self._experiments.transition(
                experiment.experiment_id,
                ExperimentState.COMPLETED,
                reason=f"deterministic factor run {factor_run.run_id} completed",
                actor="system:factor_lab",
            )
            experiment = self._experiments.transition(
                experiment.experiment_id,
                ExperimentState.VALIDATING,
                reason="statistical review started",
                actor="system:orchestrator",
            )
            statistician = self._call(
                "statistician",
                StatisticalReview,
                self._statistician_messages(candidate, factor_run),
                calls,
            )
            local_gates = self._local_gates(factor_run)
            decision = self._call(
                "orchestrator",
                OrchestratorDecision,
                self._orchestrator_messages(candidate, critic, statistician, local_gates),
                calls,
            )
            accepted = (
                critic.approved
                and all(local_gates.values())
                and statistician.robust
                and decision.recommend_candidate
            )
            target_state = ExperimentState.CANDIDATE if accepted else ExperimentState.REJECTED
            experiment = self._experiments.transition(
                experiment.experiment_id,
                target_state,
                reason=decision.rationale,
                actor="system:quality_gates",
            )
            report = self._register_report(
                trace_id=trace_id,
                experiment=experiment,
                request=request,
                candidate=candidate,
                critic=critic,
                factor_run=factor_run,
                statistician=statistician,
                decision=decision,
                local_gates=local_gates,
                calls=calls,
            )
            recorded = self._experiments.record_run(
                experiment.experiment_id,
                code_commit=self._code_commit,
                config=request.model_dump(mode="json"),
                data_snapshot_id=None,
                input_artifact_ids=[],
                output_artifact_ids=[report.artifact_id],
                random_seed=0,
                status=target_state.value,
                metrics=factor_run.metrics,
            )
            return AgentResearchResult(
                trace_id=trace_id,
                experiment=experiment,
                experiment_run=recorded,
                factor_run=factor_run,
                candidate=candidate,
                critic=critic,
                statistician=statistician,
                decision=decision,
                local_gates=local_gates,
                report_artifact_id=report.artifact_id,
                calls=calls,
            )
        except Exception as exc:
            current = self._experiments.get(experiment.experiment_id)
            if current.state not in {ExperimentState.REJECTED, ExperimentState.RETIRED}:
                self._experiments.transition(
                    experiment.experiment_id,
                    ExperimentState.REJECTED,
                    reason=str(exc)[:1000],
                    actor="system:orchestrator",
                )
            self._experiments.record_run(
                experiment.experiment_id,
                code_commit=self._code_commit,
                config=request.model_dump(mode="json"),
                data_snapshot_id=None,
                input_artifact_ids=[],
                output_artifact_ids=[],
                random_seed=0,
                status="ERROR",
                metrics={"error": str(exc)[:1000]},
            )
            if isinstance(exc, QuantSystemError):
                raise
            raise QuantSystemError(f"agent research run failed: {exc}") from exc

    def _call(
        self,
        role: AgentRoleName,
        model: type[StructuredModel],
        messages: list[LLMMessage],
        calls: list[AgentCallTrace],
    ) -> StructuredModel:
        response = self._complete_traced(role, messages, calls)
        try:
            return _parse_structured(response.content, model)
        except QuantSystemError as exc:
            repair_messages = [
                *messages,
                LLMMessage(role="assistant", content=response.content),
                LLMMessage(role="user", content=(
                    "Your previous response failed local schema validation: "
                    f"{exc}. Preserve its factual judgment, but return JSON only matching this "
                    f"schema exactly: {json.dumps(model.model_json_schema())}"
                )),
            ]
            repaired = self._complete_traced(role, repair_messages, calls)
            return _parse_structured(repaired.content, model)

    def _complete_traced(
        self,
        role: AgentRoleName,
        messages: list[LLMMessage],
        calls: list[AgentCallTrace],
    ) -> LLMResponse:
        response = self._gateway.complete(role, messages)
        calls.append(AgentCallTrace(
            role=role,
            provider=response.provider,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            raw_request_id=response.raw_request_id,
        ))
        total_tokens = sum((call.input_tokens or 0) + (call.output_tokens or 0) for call in calls)
        if len(calls) > self._config.research_budget.max_llm_calls_per_run:
            raise QuantSystemError("research LLM call budget exceeded")
        if total_tokens > self._config.research_budget.max_total_tokens_per_run:
            raise QuantSystemError("research token budget exceeded")
        return response

    def _local_gates(self, run: FactorRun) -> dict[str, bool]:
        thresholds = self._config.factor_validation
        rank_ic = run.metrics.get("rank_ic")
        coverage = run.metrics.get("coverage")
        n_ic_days = run.metrics.get("n_ic_days")
        return {
            "dsl_valid": run.status == "COMPLETED",
            "min_ic_days": isinstance(n_ic_days, int) and n_ic_days >= thresholds.min_ic_days,
            "min_coverage": isinstance(coverage, (int, float)) and coverage >= thresholds.min_coverage,
            "min_abs_rank_ic": (
                isinstance(rank_ic, (int, float))
                and abs(rank_ic) >= thresholds.min_abs_rank_ic
            ),
        }

    def _register_report(self, **metadata: Any):
        serializable = _json_value(metadata)
        report = research_report_artifact(
            producer="multi-agent-orchestrator",
            experiment_id=metadata["experiment"].experiment_id,
            code_commit=self._code_commit,
            content_hash=hash_json(serializable),
            config=metadata["request"].model_dump(mode="json"),
            metadata=serializable,
        )
        return self._registry.register(report)

    @staticmethod
    def _researcher_messages(request: AgentResearchRequest) -> list[LLMMessage]:
        return [
            LLMMessage(role="system", content=(
                "You are the researcher. Return JSON only with name, hypothesis, expression, horizon. "
                "Expression must use one outer rank(...) and only fields open, high, low, close, "
                "volume, amount, return_1d with functions rank, delta, delay, ts_mean, ts_std, abs. "
                "Signatures: rank(x), abs(x), delta(x,n), delay(x,n), ts_mean(x,n), ts_std(x,n), "
                "where n is a positive integer no greater than 252. Horizon must be an integer from "
                "1 to 20, not a target label. Example: {\"name\":\"Momentum\",\"hypothesis\":"
                "\"Trend persistence\",\"expression\":\"rank(delta(close,20))\",\"horizon\":1}. "
                "Never use future data or Python code."
            )),
            LLMMessage(role="user", content=json.dumps(request.model_dump(mode="json"), ensure_ascii=False)),
        ]

    @staticmethod
    def _researcher_repair_messages(
        request: AgentResearchRequest,
        candidate: FactorCandidate,
        error: str,
    ) -> list[LLMMessage]:
        payload = {
            "request": request.model_dump(mode="json"),
            "rejected_candidate": candidate.model_dump(),
            "local_dsl_error": error,
        }
        return [
            LLMMessage(role="system", content=(
                "Repair the factor candidate using the local DSL error. Return JSON only with name, "
                "hypothesis, expression, horizon. Use exactly rank(x), abs(x), delta(x,n), delay(x,n), "
                "ts_mean(x,n), or ts_std(x,n); n must be a positive integer <= 252."
            )),
            LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ]

    @staticmethod
    def _critic_messages(
        request: AgentResearchRequest, candidate: FactorCandidate
    ) -> list[LLMMessage]:
        payload = {"request": request.model_dump(mode="json"), "candidate": candidate.model_dump()}
        return [
            LLMMessage(role="system", content=(
                "You are a strict quant research critic. Check PIT safety, economic rationale, "
                "DSL validity risk and overfitting. Return exactly one JSON object shaped like "
                '{"approved":false,"issues":["issue"],"rationale":"reason"}. '
                "approved must be boolean and issues must be an array of strings."
            )),
            LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ]

    @staticmethod
    def _statistician_messages(
        candidate: FactorCandidate, factor_run: FactorRun
    ) -> list[LLMMessage]:
        payload = {"candidate": candidate.model_dump(), "metrics": factor_run.metrics}
        return [
            LLMMessage(role="system", content=(
                "You are the statistician. Interpret supplied deterministic metrics only; do not "
                "invent data. Return exactly one JSON object shaped like "
                '{"robust":false,"findings":["finding"],"rationale":"reason"}. '
                "robust must be boolean and findings must be an array of strings. Do not use "
                "alternative keys such as robustness or put metric objects in findings."
            )),
            LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ]

    @staticmethod
    def _orchestrator_messages(
        candidate: FactorCandidate,
        critic: CriticReview,
        statistician: StatisticalReview,
        local_gates: dict[str, bool],
    ) -> list[LLMMessage]:
        payload = {
            "candidate": candidate.model_dump(),
            "critic": critic.model_dump(),
            "statistician": statistician.model_dump(),
            "local_gates": local_gates,
        }
        return [
            LLMMessage(role="system", content=(
                "You coordinate a research-only workflow. Local gates cannot be overridden. Return "
                "exactly one JSON object shaped like {\"recommend_candidate\":false,\"rationale\":"
                "\"reason\",\"next_steps\":[\"step\"]}. recommend_candidate must be boolean and "
                "next_steps must be an array of strings. Never request live trading."
            )),
            LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ]