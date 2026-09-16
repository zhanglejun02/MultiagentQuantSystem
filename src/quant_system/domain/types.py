"""Core identifiers, time and money primitives.

All market timestamps are timezone-aware. Naive datetimes are rejected at the
domain boundary.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")
UTC = UTC

# Bars for trading day D become available after the post-close data job.
MARKET_DATA_AVAILABLE_TIME = (17, 0)

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(prefix: str = "") -> str:
    """ULID: lexicographically sortable, timestamp-prefixed unique id."""
    ts_ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ts_ms << 80) | rand
    chars = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[value & 0x1F])
        value >>= 5
    ulid = "".join(reversed(chars))
    return f"{prefix}{ulid}" if prefix else ulid


def utc_now() -> datetime:
    return datetime.now(UTC)


def cn_dt(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=CN_TZ)


def bar_available_at(trade_date: date) -> datetime:
    h, m = MARKET_DATA_AVAILABLE_TIME
    return cn_dt(trade_date, h, m)


def require_aware(dt: datetime, name: str = "timestamp") -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware, got naive datetime {dt!r}")
    return dt


def to_decimal(value: object) -> Decimal:
    """Convert via str to avoid binary-float contamination."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize_price(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


ZERO = Decimal("0")
