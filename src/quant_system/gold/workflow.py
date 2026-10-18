"""Gold deliberation workflow (V4) executed inside the shared research runtime."""

from __future__ import annotations

import json
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from quant_system.agents.config import AgentRoleName
from quant_system.agents.deliberation import (
    ANALYSTS,
    AnalystRevision,
    Disagreement,
    EvidencePackage,
    ResearchState,
    deliberate,
)
from quant_system.agents.llm import LLMMessage
from quant_system.artifacts.envelope import ArtifactEnvelope, hash_json
from quant_system.domain.enums import ArtifactStatus, ArtifactType
from quant_system.domain.exceptions import QuantSystemError
from quant_system.gold import prompts
from quant_system.gold.analysis import StrategyParameters, evaluate_gold_strategy
from quant_system.gold.evidence import (
    ADVICE_DEFINITIONS,
    MACRO_DEFINITIONS,
    GoldMacroEvidence,
    MacroEvidenceReader,
    advice_evidence,
    allowed_advice_stances,
    diagnostic_summary,
    evidence_package,
    quote_facts,
    validate_advice,
)
from quant_system.gold.ingestion import fetch_gold
from quant_system.gold.models import Quote
from quant_system.gold.schemas import (
    CriticReview,
    GoldAdviceAuditReview,
    GoldOrchestratorDecision,
    GoldResearchRequest,
    GoldStrategyProposal,
    GoldTaskRequest,
    StatisticalReview,
)
from quant_system.gold.service import GoldFetcher, GoldService, GoldSnapshot, completed_eod_date
from quant_system.research.runtime.engine import RunContext
from quant_system.research.runtime.models import EvidenceRef, Publication, TaskResult

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class GoldWorkflow:
    def __init__(
        self,
        gold: GoldService,
        *,
        fetcher: GoldFetcher | None = None,
        macro_evidence_reader: MacroEvidenceReader | None = None,
        configuration: dict[str, Any] | None = None,
    ) -> None:
        self.gold = gold
        self.fetcher = fetcher or fetch_gold
        self.macro_evidence_reader = macro_evidence_reader
        self.configuration = configuration

    def _call(
        self,
        ctx: RunContext,
        role: AgentRoleName,
        evidence: dict[str, Any],
        response_type: type[ResponseModel],
    ) -> ResponseModel:
        instruction = prompts.base_instruction(
            role,
            json.dumps(response_type.model_json_schema(), ensure_ascii=False, separators=(",", ":")),
        )
        if role in ANALYSTS:
            instruction += prompts.ANALYST_PERSPECTIVES[role] + prompts.ANALYST_RULES
            if evidence.get("task") == "analyst_revision":
                instruction += prompts.ANALYST_REVISION_RULES
        elif role == "orchestrator":
            instruction += prompts.ORCHESTRATOR_RULES
            if evidence.get("task") == "revise_investment_advice":
                instruction += prompts.ADVICE_REVISION_RULES
        elif role == "final_auditor":
            instruction = prompts.final_auditor_instruction(
                json.dumps(response_type.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
            )
        messages = [
            LLMMessage(role="system", content=instruction),
            LLMMessage(role="user", content=json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))),
        ]

        def validate(parsed: ResponseModel) -> None:
            if isinstance(parsed, GoldOrchestratorDecision):
                validate_advice(parsed, evidence)
            elif isinstance(parsed, GoldAdviceAuditReview):
                parsed.validate_pass()
            elif isinstance(parsed, ResearchState):
                parsed.validate_evidence(EvidencePackage.model_validate(evidence["evidence_package"]), role)
            elif isinstance(parsed, AnalystRevision):
                parsed.validate_revision(
                    EvidencePackage.model_validate(evidence["evidence_package"]),
                    ResearchState.model_validate(evidence["original_state"]),
                    Disagreement.model_validate(evidence["disagreement"]),
                )

        return ctx.call(
            role, messages, response_type, validator=validate, purpose=str(evidence.get("task", role))
        )

    @staticmethod
    def _materialize_advice(decision: GoldOrchestratorDecision, evidence: dict[str, Any]) -> dict[str, Any]:
        values = evidence["analysis_context"]["facts"]["investment_advice_evidence"]
        return {
            **decision.investment_advice.model_dump(),
            "reasons": [
                {**reason.model_dump(), "value": values[reason.evidence_key]}
                for reason in decision.investment_advice.reasons
            ],
            "confidence": "LOW",
            "limitations": "仅依据冻结证据，宏观覆盖与缺口见报告，无独立样本外及个人资产适配；不代表银行或金条成交报价。",
        }

    def _finalize_advice(
        self,
        ctx: RunContext,
        request: GoldTaskRequest,
        decision: GoldOrchestratorDecision,
        evidence: dict[str, Any],
    ) -> tuple[GoldOrchestratorDecision, dict[str, Any] | None, dict[str, Any]]:
        audit: dict[str, Any] = {"status": "REJECT", "rounds": [], "published_draft_hash": None, "reason": ""}
        draft = self._materialize_advice(decision, evidence)
        try:
            for round_index in range(2):
                ctx.advance("final_audit" if round_index == 0 else "final_reaudit")
                draft_hash = hash_json(draft)
                audit_evidence = {
                    "task": "final_advice_audit",
                    "analysis_context": evidence["analysis_context"],
                    "investment_advice_constraints": evidence["investment_advice_constraints"],
                    "diagnostic": evidence["evaluation"],
                    "research_cap": request.max_weight,
                    "draft_advice": draft,
                    "draft_hash": draft_hash,
                }
                review = self._call(ctx, "final_auditor", audit_evidence, GoldAdviceAuditReview)
                synthesis_receipt = next(
                    receipt
                    for receipt in reversed(ctx.caller.receipts)
                    if receipt.role == "orchestrator" and receipt.response is not None
                )
                auditor_receipt = next(
                    receipt
                    for receipt in reversed(ctx.caller.receipts)
                    if receipt.role == "final_auditor" and receipt.response is not None
                )
                assert auditor_receipt.response is not None and synthesis_receipt.response is not None
                if (
                    auditor_receipt.model == synthesis_receipt.model
                    or auditor_receipt.response.model == synthesis_receipt.response.model
                ):
                    raise QuantSystemError(
                        "final audit response must come from a different model than synthesis"
                    )
                audit["rounds"].append(
                    {
                        "round": round_index + 1,
                        "draft": draft,
                        "draft_hash": draft_hash,
                        "review": review.model_dump(),
                    }
                )
                audit["reason"] = review.rationale
                if review.decision == "PASS":
                    audit.update(status="PASS", published_draft_hash=draft_hash)
                    return decision, draft, audit
                if review.decision == "REJECT" or round_index == 1:
                    if review.decision == "REVISE":
                        audit["reason"] = "已达到一次修订上限，仍未通过独立终审；不发布建议。"
                    return decision, None, audit
                ctx.advance("advice_revision")
                revision_evidence = {
                    "task": "revise_investment_advice",
                    "request": evidence["request"],
                    "analysis_context": evidence["analysis_context"],
                    "investment_advice_constraints": evidence["investment_advice_constraints"],
                    "local_gates": evidence["local_gates"],
                    "draft_advice": draft,
                    "audit_feedback": review.model_dump(),
                    "revision_limit": 1,
                }
                decision = self._call(ctx, "orchestrator", revision_evidence, GoldOrchestratorDecision)
                draft = self._materialize_advice(decision, evidence)
        except Exception as exc:
            audit.update(
                status="UNAVAILABLE",
                reason=f"独立终审未完成，不发布建议：{type(exc).__name__}: {str(exc)[:400]}",
            )
        return decision, None, audit

    def _context(
        self,
        request: GoldResearchRequest,
        snapshot_id: str,
        snapshot: GoldSnapshot,
    ) -> tuple[dict[str, Any], Quote]:
        selected = next((quote for quote in snapshot.quotes if quote.symbol == request.symbol), None)
        spot = next(quote for quote in snapshot.quotes if quote.symbol == "Au99.99")
        today = snapshot.fetched_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        if selected is None or any(
            quote.fetched_at != snapshot.fetched_at or quote.symbol in snapshot.retained_symbols
            for quote in (selected, spot)
        ):
            raise QuantSystemError("本轮未重新取得研究对象或现货基准行情；沿用的旧报价不能作为新研究证据。")
        if any(quote.is_stale(today) for quote in (selected, spot)):
            raise QuantSystemError("本轮源站行情超过4个自然日未更新，暂停研究（长假也会触发）。")
        if selected.points[-1].date != spot.points[-1].date:
            raise QuantSystemError("研究对象与现货基准的证据日期不一致，暂停研究，不能混用不同日期的事实。")
        if len(selected.points) <= 120:
            raise QuantSystemError("研究对象少于121个观测，不足以检验允许的慢均线窗口。")
        if any(quote.points[-1].date > completed_eod_date(snapshot.fetched_at) for quote in (selected, spot)):
            raise QuantSystemError("黄金证据包含采集时尚未完成的交易日。")
        return {
            "as_of": selected.points[-1].date.isoformat(),
            "latest_fetch": snapshot.fetched_at.isoformat(),
            "snapshot_id": snapshot_id,
            "facts": {
                "selected_instrument": quote_facts(selected),
                "rmb_gold_benchmark": quote_facts(spot),
                "eod_cutoff": completed_eod_date(snapshot.fetched_at).isoformat(),
                "macro_series_collected": False,
                "intraday_prices_collected": False,
            },
            "warnings": [*snapshot.warnings, "未自动采集宏观序列或新闻，官方链接不代表本轮新增证据。"],
        }, selected

    def _snapshot_evidence(
        self,
        ctx: RunContext,
        artifact: ArtifactEnvelope,
        snapshot: GoldSnapshot,
    ) -> list[EvidenceRef]:
        sources = [artifact]
        if artifact.data_snapshot_id:
            sources.append(self.gold.registry.get(artifact.data_snapshot_id))
        return [
            ctx.evidence(
                EvidenceRef(
                    artifact_id=source.artifact_id,
                    content_hash=source.content_hash,
                    schema_version=source.schema_version,
                    kind="FACT",
                    namespace="gold",
                    available_at=max(source.created_at, snapshot.fetched_at),
                )
            )
            for source in sources
        ]

    def collect(self, ctx: RunContext, request: GoldResearchRequest) -> TaskResult:
        ctx.advance("fetch")
        artifact, snapshot = self.gold.refresh(fetched_at=ctx.clock(), fetcher=self.fetcher)
        ctx.advance("fetch", snapshot_id=artifact.artifact_id)
        self._context(request, artifact.artifact_id, snapshot)
        references = self._snapshot_evidence(ctx, artifact, snapshot)
        return TaskResult(
            outcome="COLLECTED",
            summary="黄金行情已采集并冻结。",
            payload={"snapshot_id": artifact.artifact_id},
            evidence=references,
        )

    def research(self, ctx: RunContext, request: GoldTaskRequest) -> TaskResult:
        cutoff = ctx.freeze_cutoff()
        snapshot = self.gold.load_snapshot(request.snapshot_id)
        artifact = self.gold.registry.get(request.snapshot_id)
        references = self._snapshot_evidence(ctx, artifact, snapshot)
        context, quote = self._context(request, artifact.artifact_id, snapshot)
        context["knowledge_cutoff"] = cutoff.isoformat()
        if request.macro_snapshot_id is not None:
            if self.macro_evidence_reader is None:
                raise QuantSystemError("macro evidence reader is not configured")
            macro = GoldMacroEvidence.model_validate(
                self.macro_evidence_reader(request.macro_snapshot_id, cutoff)
            )
            if macro.snapshot_id != request.macro_snapshot_id:
                raise QuantSystemError("macro evidence snapshot identity mismatch")
            macro_artifact = self.gold.registry.get(macro.snapshot_id)
            if macro_artifact.artifact_type.value != "MACRO_SNAPSHOT":
                raise QuantSystemError("macro facts must reference a macro snapshot, not a research report")
            references.extend(ctx.evidence(reference) for reference in macro.evidence)
            context["macro_snapshot_id"] = macro.snapshot_id
            context["macro_evidence"] = [reference.model_dump(mode="json") for reference in macro.evidence]
            context["facts"]["macro_items"] = {
                key: item.model_dump(mode="json") for key, item in macro.items.items()
            }
            context["facts"]["macro_series_collected"] = any(
                item.value is not None for item in macro.items.values()
            )
            context["warnings"] = [*snapshot.warnings, "宏观仅采用冻结快照中的事实，缺失类别不补造。"]
        missing_macro = [
            key
            for key in MACRO_DEFINITIONS
            if context["facts"].get("macro_items", {}).get(key, {}).get("value") is None
        ]
        context["facts"]["missing_macro_categories"] = missing_macro
        ctx.advance("evaluation", snapshot_id=request.snapshot_id)
        parameters = StrategyParameters()
        evaluation = evaluate_gold_strategy(quote, parameters, max_weight=request.max_weight)
        spot = next(item for item in snapshot.quotes if item.symbol == "Au99.99")
        advice_facts = advice_evidence(spot, evaluation)
        context["facts"]["investment_advice_evidence"] = advice_facts
        package = evidence_package(context, evaluation)
        deliberation = deliberate(
            package,
            request.model_dump(),
            analyze=lambda role, payload: self._call(ctx, role, payload, ResearchState),
            revise=lambda role, payload: self._call(ctx, role, payload, AnalystRevision),
            advance=ctx.advance,
            can_review=lambda count: ctx.ledger.can_afford(count + 4),
        )
        if missing_macro:
            deliberation = deliberation.model_copy(update={"conclusion": "INSUFFICIENT_EVIDENCE"})
        states = {state.analyst: state for state in deliberation.states}
        technical, quant, risk = (
            states[role] for role in ("technical_analyst", "quant_analyst", "risk_analyst")
        )
        proposal = GoldStrategyProposal(
            name="黄金趋势与配置研究",
            hypothesis=technical.summary,
            market_view=technical.summary,
            risks=risk.risks or ["证据不足，不能外推未来收益。"],
            parameters=parameters,
        )
        critic = CriticReview(approved=False, issues=risk.risks, rationale=risk.summary)
        statistician = StatisticalReview(robust=False, findings=quant.risks, rationale=quant.summary)
        diagnostic = evaluation["diagnostic"]
        double_cost = evaluation.get("double_cost", {})
        gates = {
            "本轮重新采集且研究对象与现货日期一致": context["facts"]["selected_instrument"]["as_of"]
            == context["facts"]["rmb_gold_benchmark"]["as_of"],
            "至少126个诊断观测": len(diagnostic.get("curve", [])) >= 126,
            "研究仓位上限大于零": request.max_weight > 0,
            "基准成本后收益为正": diagnostic.get("total_return", -1) > 0,
            "双倍成本后收益为正": double_cost.get("total_return", -1) > 0,
            "优于同仓位上限买入持有": diagnostic.get("total_return", -1)
            > diagnostic.get("buy_hold_same_cap_return", 0),
            "诊断最大回撤不超过10%": diagnostic.get("max_drawdown", -1) >= -0.10,
            "批判者未否决": critic.approved,
            "统计员未否决": statistician.robust,
            "独立样本外验证": False,
            "独立审议证据充分": deliberation.conclusion == "EVIDENCE_BOUNDED" and not missing_macro,
        }
        allowed_stances = allowed_advice_stances(advice_facts)
        if missing_macro or deliberation.conclusion == "INSUFFICIENT_EVIDENCE":
            allowed_stances = ["WAIT"]
        evidence = {
            "request": request.model_dump(),
            "analysis_context": context,
            "evaluation": diagnostic_summary(evaluation),
            "local_gates": gates,
            "investment_advice_constraints": {
                "allowed_stances": allowed_stances,
                "evidence_definitions": ADVICE_DEFINITIONS,
                "required_macro_categories": list(MACRO_DEFINITIONS),
                "missing_macro_categories": missing_macro,
            },
            "evidence_package": package.model_dump(mode="json"),
            "research_states": [state.model_dump(mode="json") for state in deliberation.states],
            "disagreements": [item.model_dump(mode="json") for item in deliberation.disagreements],
            "research_conclusion": deliberation.conclusion,
        }
        ctx.advance("orchestrator")
        decision = self._call(ctx, "orchestrator", evidence, GoldOrchestratorDecision)
        decision, advice, advice_audit = self._finalize_advice(ctx, request, decision, evidence)
        gates["统筹者未否决"] = decision.recommend_candidate
        gates["最终建议独立终审通过"] = advice_audit["status"] == "PASS"
        receipts = ctx.caller.receipts
        result = {
            "proposal": proposal.model_dump(),
            "evaluation": evaluation,
            "critic": critic.model_dump(),
            "statistician": statistician.model_dump(),
            "decision": decision.model_dump(exclude={"investment_advice"}),
            "investment_advice": advice,
            "advice_audit": advice_audit,
            "deliberation": deliberation.model_dump(mode="json"),
            "evidence_package": package.model_dump(mode="json"),
            "local_gates": gates,
            "outcome": "OBSERVE",
            "deployment_eligible": False,
            "analysis_context": context,
            "calls": [receipt.trace() for receipt in receipts if receipt.response is not None],
        }
        ctx.check()
        usage = ctx.ledger.usage()
        uri, digest = self.gold.store.put_content_addressed_json(
            "gold/research/reports",
            {
                "run_id": ctx.root_run_id,
                "task_run_id": ctx.run_id,
                "request": request.model_dump(mode="json"),
                "result": result,
                "audit": [
                    {**receipt.model_dump(mode="json"), "task": receipt.purpose} for receipt in receipts
                ],
                "configuration": self.configuration
                or {
                    "roles": {receipt.role: receipt.route for receipt in receipts},
                },
                "tokens_used": usage["tokens_used"],
                "budget": {
                    "max_calls": usage["max_calls"],
                    "max_tokens": usage["max_tokens"],
                    "dollar_cost_verified": False,
                },
                "protocol": "gold-deliberation-v4",
                "runtime_protocol": "research-runtime-v1",
            },
        )
        parents = {reference.artifact_id for reference in references}
        for owner in (ctx.root_run_id, ctx.run_id):
            revisions = [
                item
                for item in self.gold.registry.find_by_type(ArtifactType.RESEARCH_RUN)
                if item.metadata.get("run_id") == owner
            ]
            if revisions:
                parents.add(max(revisions, key=lambda item: int(item.metadata["revision"])).artifact_id)
        approved_hash = advice_audit["published_draft_hash"] if advice_audit["status"] == "PASS" else None
        ctx.check()
        report = self.gold.registry.register(
            ArtifactEnvelope(
                artifact_type=ArtifactType.GOLD_RESEARCH_REPORT,
                producer="gold-agent-research",
                created_at=ctx.clock(),
                uri=uri,
                content_hash=digest,
                data_snapshot_id=artifact.data_snapshot_id,
                parent_artifact_ids=tuple(sorted(parents)),
                status=ArtifactStatus.VERIFIED,
                metadata={
                    "run_id": ctx.root_run_id,
                    "task_run_id": ctx.run_id,
                    "research_only": True,
                    "deployment_eligible": False,
                    "approved_publication_hash": approved_hash,
                },
            )
        )
        ctx.check()
        return TaskResult(
            outcome="OBSERVE",
            payload=result,
            summary=decision.rationale,
            evidence=references,
            publication=Publication(
                decision="PASS" if approved_hash else "WITHHELD",
                content_hash=approved_hash,
                reason=advice_audit["reason"],
            ),
            report_artifact_id=report.artifact_id,
        )
