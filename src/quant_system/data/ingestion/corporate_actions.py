"""Cash-dividend reconciliation and cash-only adjustment for the baseline dataset.

Pure functions over normalized vendor rows; no HTTP and no file IO.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from quant_system.domain.exceptions import QuantSystemError


def vendor_date(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def cash_dividends(
    code: str,
    rows: list[dict[str, Any]],
    bars: list[dict[str, Any]],
    start: date,
    end: date,
) -> tuple[dict[date, Decimal], list[dict[str, Any]]]:
    cash_by_day: dict[date, Decimal] = {}
    for row in rows:
        if not row.get("ex_date") or row.get("div_proc") != "实施":
            continue
        ex_day = vendor_date(row["ex_date"])
        if not start <= ex_day <= end:
            continue
        if Decimal(str(row.get("stk_div") or 0)) != 0:
            raise QuantSystemError(f"stock distribution unsupported in baseline V1: {code}")
        cash = Decimal(str(row.get("cash_div_tax") or 0))
        if cash <= 0 or not row.get("ann_date") or vendor_date(row["ann_date"]) >= ex_day:
            raise QuantSystemError(f"invalid dividend availability: {code}/{ex_day}")
        if ex_day in cash_by_day and cash_by_day[ex_day] != cash:
            raise QuantSystemError(f"conflicting dividend records: {code}/{ex_day}")
        cash_by_day[ex_day] = cash

    reconciliations = []
    for previous, current in zip(bars, bars[1:], strict=False):
        day = current["trade_date"]
        if day not in cash_by_day:
            continue
        announced = cash_by_day[day]
        implied = Decimal(str(previous["close"])) - Decimal(str(current["prev_close"]))
        discrepancy = abs(implied - announced)
        if implied > 0 and discrepancy > Decimal("0.02"):
            if discrepancy > min(Decimal("0.10"), announced * Decimal("0.01")):
                raise QuantSystemError(f"dividend/ex-reference disagreement: {code}/{day}")
            reconciliations.append(
                {
                    "symbol": code.replace(".SH", ".SSE"),
                    "ex_date": day.isoformat(),
                    "announced_cash": str(announced),
                    "applied_cash": str(implied),
                    "basis": "daily ex-reference implied cash; small implementation variation",
                }
            )
            cash_by_day[day] = implied
    return cash_by_day, reconciliations


def cash_adjustment_factors(
    bars: list[dict[str, Any]],
    cash_by_day: dict[date, Decimal],
) -> dict[date, Decimal]:
    cumulative = Decimal("1")
    factors = {}
    for index, bar in enumerate(bars):
        day = bar["trade_date"]
        if day in cash_by_day:
            if index == 0:
                raise QuantSystemError("dividend on first dataset session lacks anchor")
            previous_close = Decimal(str(bars[index - 1]["close"]))
            cash = cash_by_day[day]
            if previous_close <= cash:
                raise QuantSystemError("invalid cash adjustment ratio")
            cumulative *= previous_close / (previous_close - cash)
        factors[day] = cumulative
    return factors


def restore_unadjusted_reference(
    code: str,
    bars: list[dict[str, Any]],
    cash_by_day: dict[date, Decimal],
    factors: dict[date, Decimal],
) -> None:
    for previous, current in zip(bars, bars[1:], strict=False):
        day = current["trade_date"]
        raw_previous = Decimal(str(previous["close"]))
        vendor_reference = Decimal(str(current["prev_close"]))
        cash = cash_by_day.get(day, Decimal("0"))
        if min(abs(vendor_reference - raw_previous), abs(vendor_reference - (raw_previous - cash))) > Decimal(
            "0.02"
        ):
            raise QuantSystemError(f"unexplained ex-price adjustment: {code}/{day}")
        if factors[day] != factors[previous["trade_date"]]:
            if day not in cash_by_day:
                raise QuantSystemError(f"unexplained adjustment jump: {code}/{day}")
            current["prev_close"] = previous["close"]
