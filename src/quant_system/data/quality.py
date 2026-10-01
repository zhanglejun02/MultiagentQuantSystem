"""Deterministic quality gates for normalized end-of-day market data."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class QualitySeverity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"


class QualityIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    severity: QualitySeverity
    dataset: str
    detail: str


class DataQualityReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    passed: bool
    issues: tuple[QualityIssue, ...]
    statistics: dict[str, int | float]


class MarketDataQualityGate:
    def evaluate(
        self,
        *,
        datasets: Mapping[str, Iterable[Mapping[str, Any]]],
        required_datasets: Iterable[str],
        optional_datasets: Iterable[str] = (),
        acquisition_errors: Iterable[str] = (),
        expected_equity_symbols: int | None = None,
        minimum_equity_coverage: float = 0.95,
    ) -> DataQualityReport:
        materialized = {name: list(rows) for name, rows in datasets.items()}
        issues: list[QualityIssue] = []

        required = set(required_datasets)
        optional = set(optional_datasets)
        for detail in sorted(acquisition_errors):
            dataset = detail.split(":", 1)[0]
            base_dataset = dataset.split("[", 1)[0]
            severity = (
                QualitySeverity.WARNING
                if base_dataset in optional and base_dataset not in required
                else QualitySeverity.ERROR
            )
            issues.append(
                QualityIssue(
                    code="ACQUISITION_FAILED",
                    severity=severity,
                    dataset=dataset,
                    detail=detail,
                )
            )

        for name in sorted(set(required_datasets)):
            if not materialized.get(name):
                issues.append(self._issue("MISSING_REQUIRED_DATASET", QualitySeverity.ERROR, name))
        for name in sorted(set(optional_datasets)):
            if not materialized.get(name):
                issues.append(self._issue("MISSING_OPTIONAL_DATASET", QualitySeverity.WARNING, name))

        equity_rows = materialized.get("equity_daily", [])
        futures_rows = materialized.get("futures_daily", [])
        issues.extend(self._validate_rows(equity_rows, "equity_daily", "trade_date"))
        issues.extend(self._validate_rows(futures_rows, "futures_daily", "trading_day"))
        issues.extend(self._validate_futures(futures_rows))

        equity_symbols = len({str(row.get("symbol", "")) for row in equity_rows if row.get("symbol")})
        if expected_equity_symbols:
            coverage = equity_symbols / expected_equity_symbols
            if coverage < minimum_equity_coverage:
                issues.append(
                    QualityIssue(
                        code="LOW_EQUITY_COVERAGE",
                        severity=QualitySeverity.ERROR,
                        dataset="equity_daily",
                        detail=(
                            f"coverage {coverage:.4f} below minimum "
                            f"{minimum_equity_coverage:.4f}"
                        ),
                    )
                )
        else:
            coverage = 0.0

        ordered = tuple(sorted(issues, key=lambda issue: (issue.severity, issue.dataset, issue.code, issue.detail)))
        return DataQualityReport(
            passed=not any(issue.severity == QualitySeverity.ERROR for issue in ordered),
            issues=ordered,
            statistics={
                "datasets": len(materialized),
                "equity_rows": len(equity_rows),
                "equity_symbols": equity_symbols,
                "equity_coverage": coverage,
                "futures_rows": len(futures_rows),
                "errors": sum(issue.severity == QualitySeverity.ERROR for issue in ordered),
                "warnings": sum(issue.severity == QualitySeverity.WARNING for issue in ordered),
            },
        )

    def _validate_rows(
        self,
        rows: list[Mapping[str, Any]],
        dataset: str,
        date_field: str,
    ) -> list[QualityIssue]:
        issues: list[QualityIssue] = []
        seen: set[tuple[object, object]] = set()
        for index, row in enumerate(rows):
            key = (row.get("symbol"), row.get(date_field))
            if key in seen:
                issues.append(self._issue("DUPLICATE_PRIMARY_KEY", QualitySeverity.ERROR, dataset, str(key)))
            seen.add(key)

            prices = [row.get(field) for field in ("open", "high", "low", "close")]
            try:
                open_price, high, low, close = (_as_float(value) for value in prices)
                valid_ohlc = (
                    min(open_price, high, low, close) > 0
                    and high >= max(open_price, close, low)
                    and low <= min(open_price, close, high)
                )
            except (TypeError, ValueError):
                valid_ohlc = False
            if not valid_ohlc:
                issues.append(self._issue("INVALID_OHLC", QualitySeverity.ERROR, dataset, f"row={index}"))

            for field in ("volume", "amount") if dataset == "equity_daily" else ("volume", "open_interest"):
                try:
                    valid_non_negative = _as_float(row.get(field)) >= 0
                except (TypeError, ValueError):
                    valid_non_negative = False
                if not valid_non_negative:
                    issues.append(
                        self._issue("INVALID_NON_NEGATIVE_FIELD", QualitySeverity.ERROR, dataset, f"row={index} field={field}")
                    )
        return issues

    def _validate_futures(self, rows: list[Mapping[str, Any]]) -> list[QualityIssue]:
        issues: list[QualityIssue] = []
        for index, row in enumerate(rows):
            for field in ("settlement", "pre_settlement"):
                try:
                    valid = _as_float(row.get(field)) > 0
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    issues.append(
                        self._issue("INVALID_SETTLEMENT", QualitySeverity.ERROR, "futures_daily", f"row={index} field={field}")
                    )
        return issues

    @staticmethod
    def _issue(
        code: str,
        severity: QualitySeverity,
        dataset: str,
        detail: str = "",
    ) -> QualityIssue:
        return QualityIssue(code=code, severity=severity, dataset=dataset, detail=detail)


def _as_float(value: object) -> float:
    if value is None:
        raise TypeError("missing numeric value")
    return float(str(value))