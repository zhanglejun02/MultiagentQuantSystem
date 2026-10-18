"""Public price-index history adapter with explicit provenance."""

import json
import ssl
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

import certifi

from quant_system.domain.exceptions import QuantSystemError


def fetch_index_history(symbol: str, start: date, end: date) -> list[dict[str, Any]]:
    if symbol != "000300.SH":
        raise QuantSystemError("Tencent baseline adapter supports CSI 300 only")
    if (end - start).days > 540:
        raise QuantSystemError("Tencent single-request baseline history is limited to 540 calendar days")
    padded = start - timedelta(days=31)
    parameter = f"sh000300,day,{padded.isoformat()},{end.isoformat()},640,qfq"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urlencode({"param": parameter})
    context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(url, timeout=30, context=context) as response:
        payload = json.load(response)
    if payload.get("code") != 0:
        raise QuantSystemError("Tencent index history request failed")
    rows = payload.get("data", {}).get("sh000300", {}).get("day", [])
    if not rows:
        raise QuantSystemError("Tencent price index history is empty")
    dates = [date.fromisoformat(row[0]) for row in rows]
    if dates != sorted(set(dates)):
        raise QuantSystemError("Tencent index sessions are duplicated or unordered")
    normalized = []
    for index, row in enumerate(rows):
        day = dates[index]
        if not start <= day <= end:
            continue
        if index == 0:
            raise QuantSystemError("Tencent index lacks previous-close anchor")
        normalized.append(
            {
                "ts_code": symbol,
                "trade_date": day.strftime("%Y%m%d"),
                "close": float(row[2]),
                "pre_close": float(rows[index - 1][2]),
            }
        )
    return normalized
