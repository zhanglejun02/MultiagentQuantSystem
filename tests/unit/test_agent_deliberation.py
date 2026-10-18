from threading import Barrier

import pytest

from quant_system.agents.deliberation import (
    AnalystRevision,
    EvidenceItem,
    EvidencePackage,
    ResearchClaim,
    ResearchState,
    deliberate,
    detect_disagreement,
)


def _package() -> EvidencePackage:
    return EvidencePackage(
        snapshot_id="snapshot-1",
        as_of="2026-10-15",
        limitations=["No holdout"],
        items={
            "trend": EvidenceItem(
                category="price", value=0.1, definition="Historical return", source="snapshot"
            ),
            "real_yield": EvidenceItem(
                category="macro", value=None, definition="Real yield", source="not collected"
            ),
            "independent_oos": EvidenceItem(
                category="diagnostic", value=False, definition="No holdout", source="local"
            ),
        },
    )


def _state(analyst="technical_analyst", view="NEUTRAL", confidence=0.6) -> ResearchState:
    return ResearchState(
        analyst=analyst,
        view=view,
        confidence=confidence,
        summary="Only price evidence",
        claims=[ResearchClaim(claim="Historical price trend", evidence_ids=["trend"], confidence=0.5)],
        risks=["No macro evidence"],
        open_questions=[],
        invalidating_conditions=["Trend reversal"],
        missing_evidence=["real_yield"] if view == "INSUFFICIENT_EVIDENCE" else [],
    )


def test_independent_calls_are_parallel_isolated_and_macro_abstains_without_a_call() -> None:
    barrier = Barrier(3, timeout=5)
    seen = []
    stages = []

    def analyze(analyst, payload):
        barrier.wait()
        seen.append(payload)
        return _state(analyst)

    result = deliberate(
        _package(),
        {"focus": "gold"},
        analyze=analyze,
        revise=lambda *_: pytest.fail("No disagreement"),
        advance=stages.append,
        can_review=lambda _: True,
    )
    assert len(seen) == 3
    assert all(set(payload) == {"task", "request", "evidence_package"} for payload in seen)
    assert all(payload["evidence_package"] == _package().model_dump(mode="json") for payload in seen)
    assert stages == ["independent_analysis", "cross_review"]
    macro = result.states[1]
    assert macro.view == "INSUFFICIENT_EVIDENCE" and macro.confidence == 0
    assert result.conclusion == "INSUFFICIENT_EVIDENCE"
    assert result.review_status == "NOT_NEEDED"
    assert "macro_analyst" in result.skipped_analysts


@pytest.mark.parametrize("reference", ["invented", "real_yield"])
def test_claims_cannot_cite_unknown_or_uncollected_facts(reference) -> None:
    state = _state().model_copy(
        update={"claims": [ResearchClaim(claim="Unsupported", evidence_ids=[reference], confidence=0.5)]}
    )
    with pytest.raises(ValueError, match="evidence"):
        state.validate_evidence(_package(), "technical_analyst")


def test_only_largest_disagreement_receives_one_targeted_author_revision() -> None:
    seen = []

    def revise(analyst, payload):
        seen.append(payload)
        return AnalystRevision(
            action="RETRACT",
            state=_state(analyst, "INSUFFICIENT_EVIDENCE", 0.3),
            addressed_disagreement_ids=["D1"],
            evidence_requests=["real_yield"],
            rationale="Missing regime evidence",
        )

    result = deliberate(
        _package(),
        {},
        analyze=lambda analyst, _: _state(analyst, "BEARISH" if analyst == "risk_analyst" else "BULLISH"),
        revise=revise,
        advance=lambda _: None,
        can_review=lambda _: True,
    )
    assert len(result.disagreements) == 1
    assert result.disagreements[0].kind == "DIRECTION"
    assert len(result.revisions) == 2
    assert {revision.state.analyst for revision in result.revisions} == {"technical_analyst", "risk_analyst"}
    for payload in seen:
        assert len(payload["peer_states"]) == 1
        assert "initial_states" not in payload
        assert payload["new_evidence_available"] is False
    assert result.initial_states[0].view == "BULLISH"
    assert result.states[0].view == "INSUFFICIENT_EVIDENCE"
    assert result.conclusion == "INSUFFICIENT_EVIDENCE"


def test_revision_cannot_increase_confidence_or_change_author() -> None:
    original = _state()
    disagreement = detect_disagreement([original, _state("risk_analyst", "BEARISH")])
    revision = AnalystRevision(
        action="MAINTAIN",
        state=_state(confidence=0.8),
        addressed_disagreement_ids=["D1"],
        evidence_requests=[],
        rationale="No new evidence",
    )
    with pytest.raises(ValueError, match="cannot increase"):
        revision.validate_revision(_package(), original, disagreement)
    with pytest.raises(ValueError, match="identity"):
        _state("risk_analyst").validate_evidence(_package(), "technical_analyst")


def test_budget_does_not_turn_skipped_review_into_consensus() -> None:
    result = deliberate(
        _package(),
        {},
        analyze=lambda analyst, _: _state(analyst, "BULLISH"),
        revise=lambda *_: pytest.fail("Budget exhausted"),
        advance=lambda _: None,
        can_review=lambda _: False,
    )
    assert result.review_status == "BUDGET_SKIPPED"
    assert result.revisions == []
    assert result.conclusion == "INSUFFICIENT_EVIDENCE"


def test_abstention_is_not_a_high_confidence_directional_answer() -> None:
    with pytest.raises(ValueError, match="abstention"):
        _state(view="INSUFFICIENT_EVIDENCE", confidence=0.9)


def test_claim_confidence_and_assigned_disagreement_are_locally_enforced() -> None:
    original = _state()
    disagreement = detect_disagreement([original, _state("risk_analyst", "BEARISH")])
    changed = original.model_copy(
        update={
            "claims": [
                ResearchClaim(
                    claim="Historical price trend",
                    evidence_ids=["trend"],
                    confidence=0.9,
                )
            ]
        }
    )
    revision = AnalystRevision(
        action="MAINTAIN",
        state=changed,
        addressed_disagreement_ids=["D1"],
        evidence_requests=[],
        rationale="No added evidence",
    )
    with pytest.raises(ValueError, match="claim confidence"):
        revision.validate_revision(_package(), original, disagreement)
    with pytest.raises(ValueError, match="assigned disagreement"):
        revision.model_copy(update={"addressed_disagreement_ids": ["invented"]}).validate_revision(
            _package(), original, disagreement
        )
