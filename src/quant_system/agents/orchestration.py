"""Auditable multi-agent factor research orchestration.

LLMs propose and review structured research. Only the local FactorLab parses
and evaluates factor expressions, and only local gates can promote a run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quant_system.agents import prompts
from quant_system.agents.config import AgentLLMConfig, AgentRoleName
from quant_system.agents.llm import CompletionGateway as CompletionGateway
from quant_system.agents.llm import LLMMessage
from quant_system.artifacts.envelope import ArtifactEnvelope, hash_json
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import ArtifactStore, LocalArtifactStore
from quant_system.artifacts.types import research_report_artifact
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.data.snapshots.service import DataSnapshotService
from quant_system.domain.enums import ExperimentState
from quant_system.domain.exceptions import QuantSystemError
from quant_system.domain.models import Bar
from quant_system.domain.types import bar_available_at, new_id
from quant_system.factors.dsl import FactorExpression, FactorExpressionError
from quant_system.factors.evaluation import FactorLab, FactorRun
from quant_system.research.experiments import Experiment, ExperimentRun, ExperimentService
from quant_system.research.runtime.calls import BudgetLedger, BudgetLimits, CallReceipt, StructuredCaller
from quant_system.research.runtime.engine import RunContext
from quant_system.research.runtime.models import EvidenceRef


class AgentResearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    topic: str = Field(min_length=3, max_length=500)
    universe_id: str = "cn_a_all"
    start_date: date
    end_date: date
    target: str = "NEXT_OPEN_TO_CLOSE"
    data_snapshot_id: str | None = None


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
    try:
        return StructuredCaller.parse(content, model)
    except QuantSystemError as exc:
        raise QuantSystemError(f"{model.__name__} {exc}") from exc


@dataclass(frozen=True)
class BoundFactorInput:
    snapshot_id: str
    content_hash: str
    path: Path
    provider: LocalParquetProvider


class _VerifiedFactorProvider(LocalParquetProvider):
    def __init__(self, bound: BoundFactorInput, provider: LocalParquetProvider) -> None:
        super().__init__(str(bound.path))
        self._source = bound.provider
        self._delegate = provider

    def get_bars(self, symbols: list[str] | None, start: date, end: date, *, as_of: datetime) -> list[Bar]:
        bars = self._delegate.get_bars(symbols, start, end, as_of=as_of)
        expected = {
            (bar.symbol, bar.trade_date, bar.available_at): bar
            for bar in self._source.get_bars(symbols, start, end, as_of=as_of)
        }
        keys = [(bar.symbol, bar.trade_date, bar.available_at) for bar in bars]
        if len(set(keys)) != len(keys) or any(
            expected.get(key) != bar for key, bar in zip(keys, bars, strict=True)
        ):
            raise QuantSystemError(
                "FactorLab provider is not bound to the requested data snapshot: bars differ"
            )
        return bars

    def trading_calendar(self, start: date, end: date) -> list[date]:
        days = self._delegate.trading_calendar(start, end)
        if not set(days).issubset(self._source.trading_calendar(start, end)):
            raise QuantSystemError(
                "FactorLab provider is not bound to the requested data snapshot: calendar differs"
            )
        return days

    def adjusted_closes(self, bars: list[Bar], *, as_of: datetime) -> dict[tuple[str, date], Decimal]:
        adjusted = self._delegate.adjusted_closes(bars, as_of=as_of)
        if adjusted != self._source.adjusted_closes(bars, as_of=as_of):
            raise QuantSystemError(
                "FactorLab provider is not bound to the requested data snapshot: adjustments differ"
            )
        return adjusted


class AgentResearchOrchestrator:
    def __init__(
        self,
        *,
        config: AgentLLMConfig,
        gateway: CompletionGateway,
        experiments: ExperimentService,
        factor_lab: FactorLab | None = None,
        registry: ArtifactRegistry,
        code_commit: str = "unknown",
        store: ArtifactStore | None = None,
        snapshot_service: DataSnapshotService | None = None,
        factor_lab_factory: Callable[[LocalParquetProvider], FactorLab] | None = None,
    ) -> None:
        self._config = config
        self._gateway = gateway
        self._experiments = experiments
        self._factor_lab = factor_lab
        self._registry = registry
        self._code_commit = code_commit
        self._store = store
        self._snapshots = snapshot_service
        if self._snapshots is None and isinstance(store, LocalArtifactStore):
            self._snapshots = DataSnapshotService(store, registry)
        self._factor_lab_factory = factor_lab_factory

    def run(self, request: AgentResearchRequest, *, context: RunContext | None = None) -> AgentResearchResult:
        if request.start_date >= request.end_date:
            raise QuantSystemError("start_date must be before end_date")
        if self._config.research_budget.max_llm_calls_per_run < 4:
            raise QuantSystemError("research budget requires at least four LLM calls")
        if context is not None and self._store is None:
            raise QuantSystemError("runtime factor research requires an artifact store for reports")
        if context is not None:
            context.check()
        factor_input = self._resolve_factor_input(request)
        factor_lab = self._resolve_factor_lab(request, factor_input)

        trace_id = new_id("atrace_")
        if context is None:
            ledger, caller = self._standalone_calls(trace_id)
        else:
            ledger, caller = context.ledger, context.caller
        if not ledger.can_afford(4):
            ledger.check()
            raise QuantSystemError("research LLM call budget exceeded; factor research requires four calls")

        def advance(stage: str) -> None:
            if context is not None:
                context.advance(stage)
            else:
                ledger.check()

        experiment = self._experiments.create(
            request.topic,
            "Four-agent PIT-safe factor research run",
            metadata={"trace_id": trace_id, "request": request.model_dump(mode="json")},
        )
        try:
            if request.data_snapshot_id is not None and context is not None:
                context.evidence(self.snapshot_evidence(request.data_snapshot_id))
            advance("candidate")
            candidate = self._call(
                "researcher",
                FactorCandidate,
                self._researcher_messages(request),
                caller,
                context,
            )
            try:
                FactorExpression(candidate.expression)
            except FactorExpressionError as exc:
                candidate = self._call(
                    "researcher",
                    FactorCandidate,
                    self._researcher_repair_messages(request, candidate, str(exc)),
                    caller,
                    context,
                )
                FactorExpression(candidate.expression)
            experiment = self._experiments.transition(
                experiment.experiment_id,
                ExperimentState.PROPOSED,
                reason="researcher produced a structured factor candidate",
                actor="agent:researcher",
            )
            experiment = self._experiments.transition(
                experiment.experiment_id,
                ExperimentState.SPEC_READY,
                reason="candidate passed local DSL validation; ready for deterministic evaluation",
                actor="system:factor_lab",
            )
            experiment = self._experiments.transition(
                experiment.experiment_id,
                ExperimentState.RUNNING,
                reason="local FactorLab evaluation started",
                actor="system:factor_lab",
            )
            advance("evaluation")
            self._verify_snapshot(factor_input)
            factor_run = factor_lab.evaluate(
                expression=candidate.expression,
                universe_id=request.universe_id,
                start_date=request.start_date,
                end_date=request.end_date,
                target=request.target,
                horizon=candidate.horizon,
            )
            self._verify_snapshot(factor_input)
            experiment = self._experiments.transition(
                experiment.experiment_id,
                ExperimentState.COMPLETED,
                reason=f"deterministic factor run {factor_run.run_id} completed",
                actor="system:factor_lab",
            )
            experiment = self._experiments.transition(
                experiment.experiment_id,
                ExperimentState.VALIDATING,
                reason="independent critic and statistical reviews started",
                actor="system:orchestrator",
            )
            critic_messages = self._critic_messages(request, candidate, factor_run)
            statistician_messages = self._statistician_messages(request, candidate, factor_run)
            advance("independent_review")
            review_tokens = sum(self._token_limits("critic", critic_messages)) + sum(
                self._token_limits("statistician", statistician_messages)
            )
            if not ledger.can_afford(2, review_tokens):
                ledger.check()
                if not ledger.can_afford(2):
                    raise QuantSystemError("research LLM call budget exceeded")
                raise QuantSystemError("research token budget exceeded")
            with ThreadPoolExecutor(max_workers=2) as pool:
                critic_future = pool.submit(
                    self._call, "critic", CriticReview, critic_messages, caller, context
                )
                statistician_future = pool.submit(
                    self._call,
                    "statistician",
                    StatisticalReview,
                    statistician_messages,
                    caller,
                    context,
                )
                critic = critic_future.result()
                statistician = statistician_future.result()
            local_gates = self._local_gates(factor_run)
            advance("orchestrator")
            decision = self._call(
                "orchestrator",
                OrchestratorDecision,
                self._orchestrator_messages(candidate, critic, statistician, local_gates),
                caller,
                context,
            )
            role_order = {
                role: index
                for index, role in enumerate(("researcher", "critic", "statistician", "orchestrator"))
            }
            calls = [
                AgentCallTrace.model_validate(receipt.trace())
                for receipt in sorted(
                    caller.receipts, key=lambda receipt: (role_order[receipt.role], receipt.sequence)
                )
                if receipt.response is not None
            ]
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
            advance("report")
            self._verify_snapshot(factor_input)
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
                call_receipts=self._receipt_metadata(trace_id, caller, context),
                usage=ledger.usage(),
                runtime_run_id=context.run_id if context is not None else None,
            )
            recorded = self._experiments.record_run(
                experiment.experiment_id,
                code_commit=self._code_commit,
                config=request.model_dump(mode="json"),
                data_snapshot_id=request.data_snapshot_id,
                input_artifact_ids=[request.data_snapshot_id] if request.data_snapshot_id else [],
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
                data_snapshot_id=request.data_snapshot_id,
                input_artifact_ids=[request.data_snapshot_id] if request.data_snapshot_id else [],
                output_artifact_ids=[],
                random_seed=0,
                status="ERROR",
                metrics={
                    "error": str(exc)[:1000],
                    "usage": ledger.usage(),
                    "call_receipts": self._receipt_metadata(trace_id, caller, context, persist=False),
                },
            )
            if isinstance(exc, QuantSystemError):
                raise
            raise QuantSystemError(f"agent research run failed: {exc}") from exc

    def _call(
        self,
        role: AgentRoleName,
        model: type[StructuredModel],
        messages: list[LLMMessage],
        caller: StructuredCaller,
        context: RunContext | None,
    ) -> StructuredModel:
        if context is not None:
            return context.call(role, messages, model, purpose=f"factor.{role}")
        return caller.call(role, messages, model, purpose=f"factor.{role}")

    def _token_limits(self, role: AgentRoleName, messages: list[LLMMessage]) -> tuple[int, int]:
        _, route = self._config.route(role)
        serialized = json.dumps([message.model_dump(mode="json") for message in messages], ensure_ascii=False)
        return len(serialized.encode("utf-8")) + 256, route.max_output_tokens

    def _standalone_calls(self, trace_id: str) -> tuple[BudgetLedger, StructuredCaller]:
        ledger = BudgetLedger(
            BudgetLimits(
                max_calls=min(30, self._config.research_budget.max_llm_calls_per_run),
                max_tokens=self._config.research_budget.max_total_tokens_per_run,
            )
        )

        def sink(receipt: CallReceipt) -> None:
            self._persist_receipt(trace_id, receipt)

        return ledger, StructuredCaller(
            self._config, self._gateway, ledger, sink=sink if self._store is not None else None
        )

    def _persist_receipt(self, trace_id: str, receipt: CallReceipt) -> dict[str, Any]:
        if self._store is None:
            return receipt.model_dump(mode="json")
        uri, digest = self._store.put_content_addressed_json(
            f"research/factor/calls/{trace_id}/{receipt.call_id}", receipt.model_dump(mode="json")
        )
        return {
            "call_id": receipt.call_id,
            "sequence": receipt.sequence,
            "status": receipt.status,
            "uri": uri,
            "content_hash": digest,
        }

    def _receipt_metadata(
        self, trace_id: str, caller: StructuredCaller, context: RunContext | None, *, persist: bool = True
    ) -> list[dict[str, Any]]:
        if context is not None:
            return [
                {
                    "run_id": context.run_id,
                    "call_id": f"{context.run_id}:{receipt.call_id}",
                    "sequence": receipt.sequence,
                    "status": receipt.status,
                }
                for receipt in caller.receipts
            ]
        if not persist:
            return [receipt.model_dump(mode="json") for receipt in caller.receipts]
        return [self._persist_receipt(trace_id, receipt) for receipt in caller.receipts]

    def _resolve_factor_input(self, request: AgentResearchRequest) -> BoundFactorInput | None:
        if request.data_snapshot_id is None:
            return None
        if self._snapshots is None:
            raise QuantSystemError("factor data_snapshot_id requires a snapshot resolver and store")
        path = self._snapshots.resolve_path(request.data_snapshot_id).resolve()
        snapshot = self._registry.get(request.data_snapshot_id)
        return BoundFactorInput(
            snapshot_id=snapshot.artifact_id,
            content_hash=snapshot.content_hash,
            path=path,
            provider=LocalParquetProvider(str(path)),
        )

    def _verify_snapshot(self, bound: BoundFactorInput | None) -> None:
        if bound is None:
            return
        if self._snapshots is None:
            raise QuantSystemError("factor data_snapshot_id requires a snapshot resolver and store")
        path = self._snapshots.resolve_path(bound.snapshot_id).resolve()
        if path != bound.path or self._registry.get(bound.snapshot_id).content_hash != bound.content_hash:
            raise QuantSystemError("factor data snapshot binding changed during research")

    def _resolve_factor_lab(self, request: AgentResearchRequest, bound: BoundFactorInput | None) -> FactorLab:
        if bound is None:
            if self._factor_lab is None:
                raise QuantSystemError("fixture research requires an injected FactorLab")
            return self._factor_lab
        lab = self._factor_lab
        if lab is None or lab._provider.data_dir.resolve() != bound.path:
            if self._factor_lab_factory is None:
                raise QuantSystemError("FactorLab provider is not bound to the requested data snapshot")
            lab = self._factor_lab_factory(LocalParquetProvider(str(bound.path)))
        if lab._provider.data_dir.resolve() != bound.path:
            raise QuantSystemError("FactorLab provider is not bound to the requested data snapshot")
        verified = _VerifiedFactorProvider(bound, lab._provider)
        warmup_start = request.start_date - timedelta(days=400)
        bars = verified.get_bars(
            None, warmup_start, request.end_date, as_of=bar_available_at(request.end_date)
        )
        verified.trading_calendar(warmup_start, request.end_date)
        if lab._adjusted_features:
            verified.adjusted_closes(bars, as_of=bar_available_at(request.end_date))
        self._verify_snapshot(bound)
        bound_lab = copy(lab)
        bound_lab._provider = bound.provider if type(lab._provider) is LocalParquetProvider else verified
        return bound_lab

    def snapshot_evidence(self, snapshot_id: str) -> EvidenceRef:
        snapshot = self._registry.get(snapshot_id)
        available_at = snapshot.created_at
        declared = snapshot.metadata.get("available_at")
        if declared is not None:
            available_at = max(available_at, datetime.fromisoformat(str(declared)))
        return EvidenceRef(
            artifact_id=snapshot.artifact_id,
            content_hash=snapshot.content_hash,
            schema_version=snapshot.schema_version,
            available_at=available_at,
            namespace=snapshot.metadata.get("namespace", "factor"),
        )

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
                isinstance(rank_ic, (int, float)) and abs(rank_ic) >= thresholds.min_abs_rank_ic
            ),
        }

    def _register_report(self, **metadata: Any) -> ArtifactEnvelope:
        serializable = _json_value({"module_id": "factor", "task_type": "factor.research", **metadata})
        uri, digest = "", hash_json(serializable)
        if self._store is not None:
            uri, digest = self._store.put_content_addressed_json("research/factor/reports", serializable)
        report = research_report_artifact(
            producer="multi-agent-orchestrator",
            experiment_id=metadata["experiment"].experiment_id,
            code_commit=self._code_commit,
            uri=uri,
            content_hash=digest,
            config=metadata["request"].model_dump(mode="json"),
            data_snapshot_id=metadata["request"].data_snapshot_id,
            parents=(metadata["request"].data_snapshot_id,) if metadata["request"].data_snapshot_id else (),
            metadata=serializable,
        )
        return self._registry.register(report)

    @staticmethod
    def _researcher_messages(request: AgentResearchRequest) -> list[LLMMessage]:
        return [
            LLMMessage(role="system", content=prompts.RESEARCHER_PROMPT),
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
            LLMMessage(role="system", content=prompts.RESEARCHER_REPAIR_PROMPT),
            LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ]

    @staticmethod
    def _critic_messages(
        request: AgentResearchRequest, candidate: FactorCandidate, factor_run: FactorRun
    ) -> list[LLMMessage]:
        payload = {
            "request": request.model_dump(mode="json", exclude={"topic"}),
            "candidate": {"expression": candidate.expression, "horizon": candidate.horizon},
            "metrics": factor_run.metrics,
        }
        return [
            LLMMessage(role="system", content=prompts.CRITIC_PROMPT),
            LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ]

    @staticmethod
    def _statistician_messages(
        request: AgentResearchRequest, candidate: FactorCandidate, factor_run: FactorRun
    ) -> list[LLMMessage]:
        payload = {
            "request": request.model_dump(mode="json", exclude={"topic"}),
            "candidate": {"expression": candidate.expression, "horizon": candidate.horizon},
            "metrics": factor_run.metrics,
        }
        return [
            LLMMessage(role="system", content=prompts.STATISTICIAN_PROMPT),
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
            LLMMessage(role="system", content=prompts.ORCHESTRATOR_PROMPT),
            LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ]
