from __future__ import annotations

from quant_system.data.quality import MarketDataQualityGate, QualitySeverity

EQUITY_ROW = {
    "symbol": "600000.SSE",
    "trade_date": "2026-09-21",
    "open": 10.0,
    "high": 10.5,
    "low": 9.9,
    "close": 10.2,
    "volume": 100,
    "amount": 1_000,
}

FUTURES_ROW = {
    "symbol": "rb2701.SHFE",
    "trading_day": "2026-09-21",
    "open": 3500,
    "high": 3550,
    "low": 3480,
    "close": 3520,
    "settlement": 3518,
    "pre_settlement": 3490,
    "volume": 100_000,
    "open_interest": 250_000,
}


def test_quality_gate_accepts_valid_core_data_with_optional_warning() -> None:
    report = MarketDataQualityGate().evaluate(
        datasets={"equity_daily": [EQUITY_ROW], "futures_daily": [FUTURES_ROW]},
        required_datasets=("equity_daily", "futures_daily"),
        optional_datasets=("industry_members",),
        expected_equity_symbols=1,
    )

    assert report.passed
    assert report.statistics["errors"] == 0
    assert [(issue.code, issue.severity) for issue in report.issues] == [
        ("MISSING_OPTIONAL_DATASET", QualitySeverity.WARNING)
    ]


def test_quality_gate_rejects_bad_rows_and_low_coverage() -> None:
    bad_equity = {**EQUITY_ROW, "high": 9.0}
    bad_futures = {**FUTURES_ROW, "settlement": None}
    report = MarketDataQualityGate().evaluate(
        datasets={
            "equity_daily": [bad_equity, bad_equity],
            "futures_daily": [bad_futures],
        },
        required_datasets=("equity_daily", "futures_daily", "trade_calendar"),
        expected_equity_symbols=10,
    )

    assert not report.passed
    codes = {issue.code for issue in report.issues}
    assert codes == {
        "DUPLICATE_PRIMARY_KEY",
        "INVALID_OHLC",
        "INVALID_SETTLEMENT",
        "LOW_EQUITY_COVERAGE",
        "MISSING_REQUIRED_DATASET",
    }


def test_quality_gate_records_acquisition_error() -> None:
    report = MarketDataQualityGate().evaluate(
        datasets={},
        required_datasets=("futures_daily",),
        acquisition_errors=("futures_daily[SHFE]: code=40203 permission denied",),
    )

    assert not report.passed
    assert any(
        issue.code == "ACQUISITION_FAILED" and "40203" in issue.detail
        for issue in report.issues
    )


def test_quality_gate_downgrades_optional_acquisition_error() -> None:
    report = MarketDataQualityGate().evaluate(
        datasets={"equity_daily": [EQUITY_ROW]},
        required_datasets=("equity_daily",),
        optional_datasets=("futures_daily",),
        acquisition_errors=("futures_daily[CFFEX]: code=40203 permission denied",),
    )

    assert report.passed
    assert any(
        issue.code == "ACQUISITION_FAILED"
        and issue.severity == QualitySeverity.WARNING
        for issue in report.issues
    )