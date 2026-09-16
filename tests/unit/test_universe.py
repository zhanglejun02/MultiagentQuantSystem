from __future__ import annotations

from datetime import date

from quant_system.data.universe.service import UniverseService
from quant_system.domain.types import cn_dt


def _universe(provider, d: date):
    svc = UniverseService(provider)
    return {m.symbol: m for m in svc.get_universe(d, as_of=cn_dt(d, 17, 30))}


def test_delisted_symbol_included_before_and_excluded_after(provider):
    before = _universe(provider, date(2024, 5, 30))
    after = _universe(provider, date(2024, 6, 3))
    assert "600004.SSE" in before  # no survivorship bias: alive at the time
    assert "600004.SSE" not in after


def test_ipo_not_in_universe_before_listing(provider):
    before = _universe(provider, date(2024, 3, 28))
    after = _universe(provider, date(2024, 4, 1))
    assert "600006.SSE" not in before
    assert "600006.SSE" in after


def test_st_and_suspension_flags(provider):
    members = _universe(provider, date(2024, 3, 12))
    assert members["600002.SSE"].is_st
    assert members["600003.SSE"].is_suspended
    assert not members["600000.SSE"].is_st


def test_status_not_known_before_available_at(provider):
    svc = UniverseService(provider)
    # suspension announced 2024-03-08 17:00; querying as_of 03-07 must not know it
    assert not svc.is_suspended(
        "600003.SSE", date(2024, 3, 11), as_of=cn_dt(date(2024, 3, 7), 17, 30)
    )
    assert svc.is_suspended(
        "600003.SSE", date(2024, 3, 11), as_of=cn_dt(date(2024, 3, 11), 9, 15)
    )
