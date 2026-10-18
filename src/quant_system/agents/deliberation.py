from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quant_system.agents.config import AgentRoleName, AnalystRole
from quant_system.artifacts.envelope import hash_json

ANALYSTS: tuple[AnalystRole, ...] = (
    "technical_analyst",
    "macro_analyst",
    "quant_analyst",
    "risk_analyst",
)
View = Literal["BULLISH", "NEUTRAL", "BEARISH", "INSUFFICIENT_EVIDENCE"]
DisagreementKind = Literal["DIRECTION", "EVIDENCE_GAP", "CONFIDENCE"]


class ResearchModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class EvidenceItem(ResearchModel):
    category: Literal["price", "diagnostic", "macro", "constraint"]
    value: float | int | bool | str | None
    definition: str = Field(min_length=1, max_length=240)
    source: str = Field(min_length=1, max_length=160)


class EvidencePackage(ResearchModel):
    snapshot_id: str
    as_of: str
    items: dict[str, EvidenceItem]
    limitations: list[str] = Field(max_length=12)

    def check_references(self, references: list[str], *, available: bool = True) -> None:
        if len(references) != len(set(references)):
            raise ValueError("evidence references must be unique")
        for reference in references:
            if reference not in self.items or (available and self.items[reference].value is None):
                raise ValueError(f"unknown or unavailable evidence: {reference}")


class ResearchClaim(ResearchModel):
    claim: str = Field(min_length=1, max_length=200)
    evidence_ids: list[str] = Field(min_length=1, max_length=4)
    counterevidence_ids: list[str] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0, le=1)


class ResearchState(ResearchModel):
    analyst: AnalystRole
    view: View
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1, max_length=240)
    claims: list[ResearchClaim] = Field(max_length=3)
    risks: list[str] = Field(max_length=4)
    open_questions: list[str] = Field(max_length=3)
    invalidating_conditions: list[str] = Field(max_length=3)
    missing_evidence: list[str] = Field(max_length=16)

    @model_validator(mode="after")
    def evidence_or_abstention(self) -> ResearchState:
        if self.view == "INSUFFICIENT_EVIDENCE":
            if not self.missing_evidence or self.confidence > 0.4:
                raise ValueError("abstention requires missing evidence and confidence at most 0.4")
        elif not self.claims:
            raise ValueError("a directional or neutral view requires evidence-backed claims")
        return self

    def validate_evidence(self, package: EvidencePackage, analyst: AgentRoleName) -> None:
        if self.analyst != analyst:
            raise ValueError("analyst identity cannot change")
        for claim in self.claims:
            package.check_references(claim.evidence_ids)
            package.check_references(claim.counterevidence_ids)
        package.check_references(self.missing_evidence, available=False)


class Disagreement(ResearchModel):
    disagreement_id: str
    kind: DisagreementKind
    analysts: tuple[AnalystRole, AnalystRole]
    question: str
    evidence_ids: list[str]


class AnalystRevision(ResearchModel):
    action: Literal["MAINTAIN", "LOWER_CONFIDENCE", "RETRACT"]
    state: ResearchState
    addressed_disagreement_ids: list[str] = Field(min_length=1, max_length=1)
    evidence_requests: list[str] = Field(max_length=16)
    rationale: str = Field(min_length=1, max_length=300)

    def validate_revision(
        self,
        package: EvidencePackage,
        original: ResearchState,
        disagreement: Disagreement,
    ) -> None:
        self.state.validate_evidence(package, original.analyst)
        package.check_references(self.evidence_requests, available=False)
        if self.addressed_disagreement_ids != [disagreement.disagreement_id]:
            raise ValueError("revision must address the assigned disagreement")
        if self.state.confidence > original.confidence:
            raise ValueError("confidence cannot increase without new evidence")
        original_confidence = {claim.claim: claim.confidence for claim in original.claims}
        ceiling = min(original.confidence, max(original_confidence.values(), default=0))
        if any(
            claim.confidence > original_confidence.get(claim.claim, ceiling) for claim in self.state.claims
        ):
            raise ValueError("claim confidence cannot increase without new evidence")
        if self.action == "MAINTAIN" and self.state.view != original.view:
            raise ValueError("MAINTAIN cannot change the view")
        if self.action == "LOWER_CONFIDENCE" and self.state.confidence >= original.confidence:
            raise ValueError("LOWER_CONFIDENCE must strictly reduce confidence")
        if self.action == "RETRACT" and self.state.view != "INSUFFICIENT_EVIDENCE":
            raise ValueError("RETRACT must explicitly abstain")


class DeliberationResult(ResearchModel):
    protocol: Literal["evidence-deliberation-v1"] = "evidence-deliberation-v1"
    evidence_hash: str
    initial_states: list[ResearchState]
    states: list[ResearchState]
    disagreements: list[Disagreement]
    revisions: list[AnalystRevision]
    skipped_analysts: dict[str, str]
    review_status: Literal["NOT_NEEDED", "REVISED", "UNRESOLVED", "BUDGET_SKIPPED"]
    conclusion: Literal["EVIDENCE_BOUNDED", "INSUFFICIENT_EVIDENCE"]


def detect_disagreement(states: list[ResearchState]) -> Disagreement | None:
    directions = {"BULLISH": 1, "NEUTRAL": 0, "BEARISH": -1}
    candidates: list[tuple[float, ResearchState, ResearchState, DisagreementKind]] = []
    for first, second in combinations(states, 2):
        if first.view == second.view == "INSUFFICIENT_EVIDENCE":
            continue
        if "INSUFFICIENT_EVIDENCE" in {first.view, second.view}:
            if {first.view, second.view} & {"BULLISH", "BEARISH"}:
                candidates.append((1.0, first, second, "EVIDENCE_GAP"))
            continue
        distance = abs(directions[first.view] - directions[second.view])
        if distance:
            candidates.append(
                (distance + min(first.confidence, second.confidence), first, second, "DIRECTION")
            )
        elif abs(first.confidence - second.confidence) >= 0.3:
            candidates.append((0.5, first, second, "CONFIDENCE"))
    if not candidates:
        return None
    _, first, second, kind = max(candidates, key=lambda candidate: candidate[0])
    references = sorted(
        {
            reference
            for state in (first, second)
            for claim in state.claims
            for reference in (*claim.evidence_ids, *claim.counterevidence_ids)
        }
    )
    return Disagreement(
        disagreement_id="D1",
        kind=kind,
        analysts=(first.analyst, second.analyst),
        question=(
            f"{first.analyst}={first.view}，{second.analyst}={second.view}。"
            "这些结论的证据范围是否一致？只回应对方引用、反例与缺口；无法补证时降低置信度或撤回。"
        ),
        evidence_ids=references,
    )


def deliberate(
    package: EvidencePackage,
    request: dict[str, Any],
    *,
    analyze: Callable[[AnalystRole, dict[str, Any]], ResearchState],
    revise: Callable[[AnalystRole, dict[str, Any]], AnalystRevision],
    advance: Callable[[str], None],
    can_review: Callable[[int], bool],
) -> DeliberationResult:
    skipped: dict[str, str] = {}
    initial: dict[AnalystRole, ResearchState] = {}
    macro_missing = [
        key for key, item in package.items.items() if item.category == "macro" and item.value is None
    ]
    if not any(item.category == "macro" and item.value is not None for item in package.items.values()):
        skipped["macro_analyst"] = "未采集宏观证据，本地记录弃权，不调用模型生成宏观判断。"
        initial["macro_analyst"] = ResearchState(
            analyst="macro_analyst",
            view="INSUFFICIENT_EVIDENCE",
            confidence=0,
            summary=skipped["macro_analyst"],
            claims=[],
            risks=["价格趋势不能证明宏观因果。"],
            open_questions=["实际利率、美元、资金流是否支持当前价格方向？"],
            invalidating_conditions=["补齐具有可用时间戳的宏观序列后重新研究。"],
            missing_evidence=macro_missing,
        )

    def independent(analyst: AnalystRole) -> ResearchState:
        state = analyze(
            analyst,
            {
                "task": "independent_analysis",
                "request": request,
                "evidence_package": package.model_dump(mode="json"),
            },
        )
        state.validate_evidence(package, analyst)
        return state

    advance("independent_analysis")
    active = [analyst for analyst in ANALYSTS if analyst not in skipped]
    with ThreadPoolExecutor(max_workers=len(active)) as executor:
        initial.update(zip(active, executor.map(independent, active), strict=True))
    states = [initial[analyst] for analyst in ANALYSTS]
    advance("cross_review")
    disagreement = detect_disagreement(states)
    revisions: list[AnalystRevision] = []
    status: Literal["NOT_NEEDED", "REVISED", "UNRESOLVED", "BUDGET_SKIPPED"] = "NOT_NEEDED"
    if disagreement:
        targets = [
            analyst
            for analyst in disagreement.analysts
            if analyst not in skipped and initial[analyst].view != "INSUFFICIENT_EVIDENCE"
        ]
        if not can_review(len(targets)):
            status = "BUDGET_SKIPPED"
        elif targets:
            advance("analyst_revision")
            for analyst in targets:
                revision = revise(
                    analyst,
                    {
                        "task": "analyst_revision",
                        "request": request,
                        "evidence_package": package.model_dump(mode="json"),
                        "original_state": initial[analyst].model_dump(mode="json"),
                        "disagreement": disagreement.model_dump(mode="json"),
                        "peer_states": [
                            initial[peer].model_dump(mode="json")
                            for peer in disagreement.analysts
                            if peer != analyst
                        ],
                        "revision_limit": 1,
                        "new_evidence_available": False,
                    },
                )
                revision.validate_revision(package, initial[analyst], disagreement)
                revisions.append(revision)
            revised = {revision.state.analyst: revision.state for revision in revisions}
            states = [revised.get(state.analyst, state) for state in states]
            status = "UNRESOLVED" if detect_disagreement(states) else "REVISED"
    insufficient = any(state.view == "INSUFFICIENT_EVIDENCE" for state in states)
    return DeliberationResult(
        evidence_hash=hash_json(package.model_dump(mode="json")),
        initial_states=[initial[analyst] for analyst in ANALYSTS],
        states=states,
        disagreements=[disagreement] if disagreement else [],
        revisions=revisions,
        skipped_analysts=skipped,
        review_status=status,
        conclusion="INSUFFICIENT_EVIDENCE"
        if insufficient or status in {"UNRESOLVED", "BUDGET_SKIPPED"}
        else "EVIDENCE_BOUNDED",
    )
