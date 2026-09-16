from __future__ import annotations

from datetime import date

import pytest

from quant_system.backtest.metrics import compute_prediction_ic
from quant_system.backtest.request import PredictionTarget, PredictionTargetSpec


def test_prediction_target_specs_produce_distinct_named_metrics():
    d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    scores = {d1: {"A": 1.0, "B": 2.0, "C": 3.0}}
    opens = {
        d1: {"A": 10, "B": 10, "C": 10},
        d2: {"A": 8, "B": 10, "C": 12},
        d3: {"A": 12, "B": 10, "C": 8},
    }
    closes = {
        d1: {"A": 10, "B": 10, "C": 10},
        d2: {"A": 9, "B": 10, "C": 11},
        d3: {"A": 8, "B": 10, "C": 12},
    }
    open_close = compute_prediction_ic(
        scores, opens, closes,
        PredictionTargetSpec(target=PredictionTarget.NEXT_OPEN_TO_CLOSE),
    )
    close_close = compute_prediction_ic(
        scores, opens, closes,
        PredictionTargetSpec(target=PredictionTarget.NEXT_CLOSE_TO_CLOSE),
    )
    assert open_close["prediction_target"] == "NEXT_OPEN_TO_CLOSE"
    assert close_close["prediction_target"] == "NEXT_CLOSE_TO_CLOSE"
    assert set(open_close) != set(close_close)
    assert open_close["rank_ic_next_open_to_close_h1_mean"] == pytest.approx(-1.0)
    assert close_close["rank_ic_next_close_to_close_h1_mean"] == pytest.approx(1.0)