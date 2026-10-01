from __future__ import annotations

from datetime import date

import pytest

from quant_system.factors.dsl import FactorExpression, FactorExpressionError


def test_factor_expression_evaluates_cross_sectional_scores():
    days = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    fields = {
        "close": {
            days[0]: {"A": 10.0, "B": 10.0},
            days[1]: {"A": 11.0, "B": 9.0},
            days[2]: {"A": 13.0, "B": 8.0},
        }
    }
    expression = FactorExpression("rank(delta(close, 1))")

    assert expression.inputs == ["close"]
    assert expression.scores(symbols=["A", "B"], index=2, days=days, fields=fields) == {
        "A": 2.0,
        "B": -1.0,
    }


@pytest.mark.parametrize(
    "expression",
    [
        "delta(close, 5)",
        "rank(__import__('os'))",
        "rank(close.__class__)",
        "rank(close ** 2)",
        "rank(close, 5)",
        "rank(rank(close))",
        "rank(delta(close))",
        "rank(ts_mean(close, 0))",
    ],
)
def test_factor_expression_rejects_unsafe_or_unsupported_syntax(expression: str):
    with pytest.raises(FactorExpressionError):
        FactorExpression(expression)