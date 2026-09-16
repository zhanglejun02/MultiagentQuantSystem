"""Deterministic synthetic fixture data for tests and the sample backtest.

Scenarios covered: normal stocks, ST, suspension window, delisting, pinned
limit-up/limit-down days, STAR/ChiNext boards (20% limits), mid-period IPO and
a low-liquidity symbol (exercises participation-limited partial fills).

Synthetic data. Not real market data; not claimed to be realistic.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from quant_system.domain.types import bar_available_at, cn_dt

TICK = Decimal("0.01")

START = date(2024, 1, 1)
END = date(2024, 6, 28)

LIMIT_UP_DAY = date(2024, 3, 5)
LIMIT_DOWN_DAY = date(2024, 4, 2)
SUSPEND_FROM = date(2024, 3, 11)
SUSPEND_TO = date(2024, 3, 22)
ST_FROM = date(2024, 3, 1)
DELIST_DATE = date(2024, 5, 31)
IPO_DATE = date(2024, 4, 1)

SYMBOLS = {
    "600000.SSE": {"exchange": "SSE", "board": "MAIN", "start_price": "10.00"},
    "600001.SSE": {"exchange": "SSE", "board": "MAIN", "start_price": "20.00"},
    "600002.SSE": {"exchange": "SSE", "board": "MAIN", "start_price": "8.00"},   # becomes ST
    "600003.SSE": {"exchange": "SSE", "board": "MAIN", "start_price": "15.00"},  # suspension window
    "600004.SSE": {"exchange": "SSE", "board": "MAIN", "start_price": "12.00"},  # delists
    "600005.SSE": {"exchange": "SSE", "board": "MAIN", "start_price": "30.00"},  # pinned limit days
    "688001.SSE": {"exchange": "SSE", "board": "STAR", "start_price": "50.00"},
    "300001.SZSE": {"exchange": "SZSE", "board": "CHINEXT", "start_price": "25.00"},
    "000001.SZSE": {"exchange": "SZSE", "board": "MAIN", "start_price": "18.00"},
    "000002.SZSE": {"exchange": "SZSE", "board": "MAIN", "start_price": "9.00"},  # low volume
    "600006.SSE": {"exchange": "SSE", "board": "MAIN", "start_price": "40.00"},  # IPO mid-period
}

LOW_VOLUME_SYMBOL = "000002.SZSE"


def trading_days(start: date = START, end: date = END) -> list[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _q(x: Decimal) -> Decimal:
    return x.quantize(TICK, rounding=ROUND_HALF_UP)


def generate_fixture_data(dest_dir: str, seed: int = 42) -> str:
    rng = random.Random(seed)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    days = trading_days()

    rows: list[dict] = []
    for symbol, cfg in SYMBOLS.items():
        prev_close = Decimal(cfg["start_price"])
        for d in days:
            if symbol == "600003.SSE" and SUSPEND_FROM <= d <= SUSPEND_TO:
                continue  # suspended: no bar
            if symbol == "600004.SSE" and d >= DELIST_DATE:
                continue  # delisted
            if symbol == "600006.SSE" and d < IPO_DATE:
                continue  # not yet listed

            if symbol == "600005.SSE" and d == LIMIT_UP_DAY:
                px = _q(prev_close * Decimal("1.10"))
                o = h = low = c = px
                volume = 100_000  # sealed at limit: thin printed volume
            elif symbol == "600005.SSE" and d == LIMIT_DOWN_DAY:
                px = _q(prev_close * Decimal("0.90"))
                o = h = low = c = px
                volume = 100_000
            else:
                drift = Decimal(str(rng.uniform(-0.025, 0.025)))
                c = _q(prev_close * (Decimal(1) + drift))
                gap = Decimal(str(rng.uniform(-0.008, 0.008)))
                o = _q(prev_close * (Decimal(1) + gap))
                h = _q(max(o, c) * (Decimal(1) + Decimal(str(rng.uniform(0, 0.006)))))
                low = _q(min(o, c) * (Decimal(1) - Decimal(str(rng.uniform(0, 0.006)))))
                if symbol == LOW_VOLUME_SYMBOL:
                    volume = rng.randint(3_000, 8_000)
                else:
                    volume = rng.randint(1_000_000, 5_000_000)
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": d,
                    "open": float(o),
                    "high": float(h),
                    "low": float(low),
                    "close": float(c),
                    "prev_close": float(prev_close),
                    "volume": volume,
                    "amount": float(c * volume),
                    "available_at": bar_available_at(d),
                }
            )
            prev_close = c

    table = pa.Table.from_pylist(rows)
    pq.write_table(table, dest / "bars.parquet")

    known_from = cn_dt(date(2023, 12, 15), 9, 0).isoformat()
    securities = []
    for symbol, cfg in SYMBOLS.items():
        list_date = IPO_DATE if symbol == "600006.SSE" else date(2020, 1, 6)
        available = (
            cn_dt(date(2024, 3, 25), 9, 0).isoformat() if symbol == "600006.SSE" else known_from
        )
        securities.append(
            {
                "symbol": symbol,
                "name": f"Fixture {symbol.split('.')[0]}",
                "exchange": cfg["exchange"],
                "board": cfg["board"],
                "security_type": "STOCK",
                "list_date": list_date.isoformat(),
                "delist_date": DELIST_DATE.isoformat() if symbol == "600004.SSE" else None,
                "available_at": available,
            }
        )
    (dest / "securities.json").write_text(json.dumps(securities, indent=1))

    statuses = [
        {
            "symbol": "600002.SSE",
            "flag": "ST",
            "effective_from": ST_FROM.isoformat(),
            "effective_to": None,
            "available_at": cn_dt(date(2024, 2, 29), 17, 0).isoformat(),
        },
        {
            "symbol": "600003.SSE",
            "flag": "SUSPENDED",
            "effective_from": SUSPEND_FROM.isoformat(),
            "effective_to": SUSPEND_TO.isoformat(),
            "available_at": cn_dt(date(2024, 3, 8), 17, 0).isoformat(),
        },
    ]
    (dest / "status_intervals.json").write_text(json.dumps(statuses, indent=1))
    return str(dest)
