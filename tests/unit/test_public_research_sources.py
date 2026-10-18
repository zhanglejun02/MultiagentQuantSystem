import json
from datetime import date
from io import BytesIO

import pytest

from quant_system.data.ingestion.eastmoney import fetch_cash_dividends, fetch_company_master
from quant_system.data.ingestion.tencent import fetch_index_history
from quant_system.domain.exceptions import QuantSystemError


def test_tencent_index_uses_previous_session_anchor(monkeypatch):
    payload = {
        "code": 0,
        "data": {
            "sh000300": {
                "day": [
                    ["2024-12-31", "100", "100", "100", "100", "1000"],
                    ["2025-01-02", "100", "110", "110", "100", "1000"],
                ]
            }
        },
    }
    monkeypatch.setattr(
        "quant_system.data.ingestion.tencent.urlopen",
        lambda *args, **kwargs: BytesIO(json.dumps(payload).encode()),
    )
    rows = fetch_index_history("000300.SH", date(2025, 1, 2), date(2025, 1, 2))
    assert rows == [{"ts_code": "000300.SH", "trade_date": "20250102", "close": 110, "pre_close": 100}]
    with pytest.raises(QuantSystemError, match="CSI 300"):
        fetch_index_history("UNKNOWN", date(2025, 1, 2), date(2025, 1, 2))


def test_eastmoney_master_requires_all_basket_members(monkeypatch):
    payload = {
        "success": True,
        "result": {
            "data": [
                {
                    "SECURITY_CODE": "600036",
                    "SECURITY_NAME_ABBR": "Bank",
                    "SECURITY_TYPE": "上交所主板A股",
                    "LISTING_DATE": "2002-04-09 00:00:00",
                }
            ]
        },
    }
    monkeypatch.setattr(
        "quant_system.data.ingestion.eastmoney.urlopen",
        lambda *args, **kwargs: BytesIO(json.dumps(payload).encode()),
    )
    assert fetch_company_master(("600036.SH",))[0]["list_date"] == "20020409"
    with pytest.raises(QuantSystemError, match="missing requested"):
        fetch_company_master(("600036.SH", "600519.SH"))


def test_eastmoney_cash_unit_and_announcement_normalization(monkeypatch):
    payload = {
        "success": True,
        "result": {
            "pages": 1,
            "data": [
                {
                    "SECURITY_CODE": "600036",
                    "ASSIGN_PROGRESS": "实施分配",
                    "EX_DIVIDEND_DATE": "2025-07-01 00:00:00",
                    "NOTICE_DATE": "2025-06-20 00:00:00",
                    "PRETAX_BONUS_RMB": 20.5,
                    "BONUS_IT_RATIO": 0,
                }
            ],
        },
    }
    monkeypatch.setattr(
        "quant_system.data.ingestion.eastmoney.urlopen",
        lambda *args, **kwargs: BytesIO(json.dumps(payload).encode()),
    )
    rows = fetch_cash_dividends(("600036.SH",), date(2025, 1, 1), date(2025, 12, 31))
    assert rows[0]["cash_div_tax"] == "2.05"
    assert rows[0]["ann_date"] == "20250620"


@pytest.mark.parametrize("report", ["company master", "dividend history"])
def test_eastmoney_failed_response_preserves_request_context(monkeypatch, report):
    monkeypatch.setattr(
        "quant_system.data.ingestion.eastmoney.urlopen",
        lambda *args, **kwargs: BytesIO(b'{"success": false}'),
    )
    with pytest.raises(QuantSystemError, match=f"Eastmoney {report} request failed"):
        if report == "company master":
            fetch_company_master(("600036.SH",))
        else:
            fetch_cash_dividends(("600036.SH",), date(2025, 1, 1), date(2025, 12, 31))
