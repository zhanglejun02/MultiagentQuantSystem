from __future__ import annotations

from datetime import date

import pytest

from quant_system.data.pit.store import InMemoryPITStore, PITRecord
from quant_system.domain.types import cn_dt

D = date(2024, 3, 4)


def test_same_day_bar_not_visible_before_available_at(provider):
    before = cn_dt(D, 16, 0)  # bars publish at 17:00
    bars = provider.get_bars(["600000.SSE"], D, D, as_of=before)
    assert bars == []


def test_same_day_bar_visible_after_available_at(provider):
    after = cn_dt(D, 17, 30)
    bars = provider.get_bars(["600000.SSE"], D, D, as_of=after)
    assert len(bars) == 1
    assert bars[0].trade_date == D


def test_history_query_never_returns_future(provider):
    as_of = cn_dt(D, 17, 30)
    bars = provider.get_bars(None, date(2024, 1, 1), date(2024, 6, 28), as_of=as_of)
    assert all(b.available_at <= as_of for b in bars)
    assert max(b.trade_date for b in bars) == D


def test_naive_as_of_rejected(provider):
    from datetime import datetime

    with pytest.raises(ValueError):
        provider.get_bars(None, D, D, as_of=datetime(2024, 3, 4, 17, 30))


def test_pit_store_as_of_filtering():
    store = InMemoryPITStore()
    t1, t2, t3 = cn_dt(D, 10, 0), cn_dt(D, 12, 0), cn_dt(D, 18, 0)
    for i, avail in enumerate([t1, t3]):
        store.append(
            PITRecord(
                key="600000.SSE", field="eps", value=i,
                event_time=t1, effective_time=t1, available_at=avail, ingested_at=avail,
            )
        )
    assert [r.value for r in store.query("600000.SSE", "eps", as_of=t2)] == [0]
    latest = store.latest("600000.SSE", "eps", as_of=cn_dt(D, 19, 0))
    assert latest is not None and latest.value == 1
