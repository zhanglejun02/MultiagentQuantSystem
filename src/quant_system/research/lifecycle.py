"""Experiment lifecycle state machine. Status can only change through
validated transitions — never by arbitrary code writing a field."""

from __future__ import annotations

from quant_system.domain.enums import ExperimentState
from quant_system.domain.exceptions import InvalidTransitionError

S = ExperimentState

ALLOWED_TRANSITIONS: dict[ExperimentState, frozenset[ExperimentState]] = {
    S.IDEA: frozenset({S.PROPOSED, S.REJECTED, S.RETIRED}),
    S.PROPOSED: frozenset({S.SPEC_READY, S.REJECTED, S.RETIRED}),
    S.SPEC_READY: frozenset({S.RUNNING, S.REJECTED, S.RETIRED}),
    S.RUNNING: frozenset({S.COMPLETED, S.REJECTED}),
    S.COMPLETED: frozenset({S.VALIDATING, S.REJECTED, S.RETIRED}),
    S.VALIDATING: frozenset({S.CANDIDATE, S.REJECTED}),
    S.CANDIDATE: frozenset({S.PAPER_TRADING, S.REJECTED, S.RETIRED}),
    S.PAPER_TRADING: frozenset({S.LIVE_CANARY, S.SUSPENDED, S.REJECTED, S.RETIRED}),
    S.LIVE_CANARY: frozenset({S.LIVE, S.SUSPENDED, S.RETIRED}),
    S.LIVE: frozenset({S.SUSPENDED, S.RETIRED}),
    S.SUSPENDED: frozenset({S.PAPER_TRADING, S.LIVE_CANARY, S.LIVE, S.RETIRED}),
    S.REJECTED: frozenset({S.RETIRED}),
    S.RETIRED: frozenset(),
}


def validate_transition(current: ExperimentState, target: ExperimentState) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(f"experiment transition {current} -> {target} not allowed")
