from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from quant_system.domain.exceptions import QuantSystemError
from quant_system.gold.models import ETF_NAMES, PricePoint, Quote
from quant_system.infra.http import request_json


def fetch_json(url: str, data: bytes | None = None) -> Any:
    return request_json(
        url,
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.sge.com.cn/sjzx/mrhq" if "sge.com.cn" in url else "https://gu.qq.com/",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=30,
    )


def parse_spot(payload: Any, as_of: date, fetched_at: datetime) -> Quote:
    rows = payload.get("time", [])
    start = as_of - timedelta(days=730)
    points = [
        PricePoint(date=row[0], open=row[1], close=row[2], low=row[3], high=row[4])
        for row in rows
        if start <= date.fromisoformat(row[0]) <= as_of
    ]
    return Quote(
        symbol="Au99.99",
        name="黄金现货 Au99.99",
        kind="spot",
        unit="CNY/g",
        source="上海黄金交易所",
        source_url="https://www.sge.com.cn/sjzx/mrhq",
        fetched_at=fetched_at,
        points=points,
    )


def parse_etf(payload: Any, symbol: str, as_of: date, fetched_at: datetime) -> Quote:
    if payload.get("code") != 0:
        raise QuantSystemError("ETF history request failed")
    code = ("sh" if symbol.startswith("5") else "sz") + symbol
    rows = payload.get("data", {}).get(code, {}).get("day", [])
    start = as_of - timedelta(days=730)
    points = [
        PricePoint(date=row[0], open=row[1], close=row[2], high=row[3], low=row[4])
        for row in rows
        if start <= date.fromisoformat(row[0]) <= as_of
    ]
    return Quote(
        symbol=symbol,
        name=ETF_NAMES[symbol],
        kind="etf",
        unit="CNY/share",
        source="腾讯证券行情",
        source_url=f"https://gu.qq.com/{code}",
        fetched_at=fetched_at,
        points=points,
    )


def fetch_gold(as_of: date, fetched_at: datetime) -> tuple[list[Quote], dict[str, Any], list[str]]:
    raw: dict[str, Any] = {"as_of": as_of.isoformat(), "fetched_at": fetched_at.isoformat(), "responses": {}}
    spot = fetch_json("https://www.sge.com.cn/graph/Dailyhq", urlencode({"instid": "Au99.99"}).encode())
    raw["responses"]["Au99.99"] = spot
    quotes = [parse_spot(spot, as_of, fetched_at)]
    warnings = [
        "观察窗口为截止日期前730个自然日；原始完整响应另行冻结，未修改历史坏值。",
        "ETF 为未复权收盘价收益，不含分红；成交量与成交额未核定单位，暂不展示。",
        "陈旧标记采用超过4个自然日的保守规则，法定长假可能触发。",
        "公开历史接口未提供逐日首次发布时间；本快照仅用于观察，不作为已认证PIT回测数据。",
    ]
    for symbol in ETF_NAMES:
        code = ("sh" if symbol.startswith("5") else "sz") + symbol
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urlencode(
            {"param": f"{code},day,,,640,"}
        )
        try:
            payload = fetch_json(url)
            raw["responses"][symbol] = payload
            quotes.append(parse_etf(payload, symbol, as_of, fetched_at))
        except Exception as exc:
            warnings.append(f"{symbol}: {type(exc).__name__}: {exc}")
    return quotes, raw, warnings
