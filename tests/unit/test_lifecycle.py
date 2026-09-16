from __future__ import annotations

import pytest

from quant_system.domain.enums import ExperimentState as S
from quant_system.domain.exceptions import InvalidTransitionError, QuantSystemError
from quant_system.research.experiments import ExperimentService
from quant_system.research.lifecycle import validate_transition


def test_full_happy_path_transitions():
    path = [S.IDEA, S.PROPOSED, S.SPEC_READY, S.RUNNING, S.COMPLETED,
            S.VALIDATING, S.CANDIDATE, S.PAPER_TRADING, S.LIVE_CANARY, S.LIVE, S.RETIRED]
    for cur, nxt in zip(path, path[1:], strict=False):
        validate_transition(cur, nxt)


@pytest.mark.parametrize(
    ("cur", "nxt"),
    [
        (S.IDEA, S.LIVE),
        (S.RUNNING, S.CANDIDATE),
        (S.COMPLETED, S.LIVE),
        (S.RETIRED, S.IDEA),
        (S.REJECTED, S.LIVE),
        (S.VALIDATING, S.PAPER_TRADING),
    ],
)
def test_invalid_transitions_rejected(cur, nxt):
    with pytest.raises(InvalidTransitionError):
        validate_transition(cur, nxt)


def test_experiment_service_transitions_and_history(session_factory):
    svc = ExperimentService(session_factory)
    exp = svc.create("test", "desc")
    assert exp.state == S.IDEA
    svc.transition(exp.experiment_id, S.PROPOSED, reason="ok")
    with pytest.raises(InvalidTransitionError):
        svc.transition(exp.experiment_id, S.LIVE)
    hist = svc.history(exp.experiment_id)
    assert len(hist) == 1
    assert hist[0]["from"] == "IDEA" and hist[0]["to"] == "PROPOSED"


def test_experiment_run_recorded(session_factory):
    svc = ExperimentService(session_factory)
    exp = svc.create("run test")
    run = svc.record_run(
        exp.experiment_id, code_commit="abc", config={"x": 1}, data_snapshot_id=None,
        input_artifact_ids=[], output_artifact_ids=["art_1"], random_seed=42,
        status="SUCCEEDED", metrics={"sharpe": 1.0},
    )
    runs = svc.runs(exp.experiment_id)
    assert len(runs) == 1
    assert runs[0].run_id == run.run_id
    assert runs[0].config_hash == run.config_hash
    assert runs[0].random_seed == 42


def test_missing_experiment_raises(session_factory):
    svc = ExperimentService(session_factory)
    with pytest.raises(QuantSystemError):
        svc.get("exp_missing")
