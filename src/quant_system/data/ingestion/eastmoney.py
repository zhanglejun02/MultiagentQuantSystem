"""Public current company reference data, never a historical universe claim."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from quant_system.domain.exceptions import QuantSystemError
from quant_system.infra.http import request_json


def _fetch_report(params: dict[str, str], *, error_message: str) -> dict[str, Any]:
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urlencode(params)
    payload = request_json(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    if not payload.get("success"):
        raise QuantSystemError(error_message)
    return payload.get("result") or {}


def fetch_company_master(symbols: tuple[str, ...]) -> list[dict[str, Any]]:
    codes = ",".join(f'"{symbol.split(".")[0]}"' for symbol in symbols)
    params = {
        "reportName": "RPT_F10_BASIC_ORGINFO",
        "columns": "ALL",
        "filter": f"(SECURITY_CODE in ({codes}))",
        "pageSize": "100",
        "pageNumber": "1",
    }
    report = _fetch_report(params, error_message="Eastmoney company master request failed")
    result = []
    for row in report.get("data", []):
        if row.get("SECURITY_TYPE") != "上交所主板A股":
            raise QuantSystemError("Eastmoney reference is not SSE main-board A share")
        listed = datetime.fromisoformat(row["LISTING_DATE"]).date()
        result.append(
            {
                "ts_code": f"{row['SECURITY_CODE']}.SH",
                "name": row["SECURITY_NAME_ABBR"],
                "market": "主板",
                "exchange": "SSE",
                "list_date": listed.strftime("%Y%m%d"),
                "delist_date": None,
            }
        )
    if {row["ts_code"] for row in result} != set(symbols):
        raise QuantSystemError("Eastmoney company master missing requested basket members")
    return sorted(result, key=lambda row: row["ts_code"])


def fetch_cash_dividends(symbols: tuple[str, ...], start: date, end: date) -> list[dict[str, Any]]:
    codes = ",".join(f'"{symbol.split(".")[0]}"' for symbol in symbols)
    params = {
        "reportName": "RPT_SHAREBONUS_DET",
        "columns": "ALL",
        "filter": f"(SECURITY_CODE in ({codes}))(EX_DIVIDEND_DATE>='{start}')(EX_DIVIDEND_DATE<='{end}')",
        "pageSize": "500",
        "pageNumber": "1",
    }
    result = _fetch_report(params, error_message="Eastmoney dividend history request failed")
    if result.get("pages", 1) > 1:
        raise QuantSystemError("dividend history exceeds single-page baseline bound")
    rows = []
    for row in result.get("data", []):
        if row.get("ASSIGN_PROGRESS") != "实施分配":
            continue
        rows.append(
            {
                "ts_code": f"{row['SECURITY_CODE']}.SH",
                "div_proc": "实施",
                "ex_date": datetime.fromisoformat(row["EX_DIVIDEND_DATE"]).strftime("%Y%m%d"),
                "ann_date": datetime.fromisoformat(row["NOTICE_DATE"]).strftime("%Y%m%d"),
                "cash_div_tax": str(Decimal(str(row.get("PRETAX_BONUS_RMB") or 0)) / 10),
                "stk_div": float(row.get("BONUS_IT_RATIO") or 0),
            }
        )
    return rows
