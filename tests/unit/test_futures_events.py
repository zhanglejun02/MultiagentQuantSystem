from datetime import date

import pytest

from quant_system.domain.types import cn_dt
from quant_system.futures.events import FuturesEvent, stamp_event, verify_event_chain


def test_futures_event_chain_rejects_tampering() -> None:
    first = stamp_event(
        FuturesEvent(
            simulation_id="fsim_test",
            seq=1,
            event_type="SIMULATION_CREATED",
            occurred_at=cn_dt(date(2026, 9, 17), 9),
            payload={"balance": "100000"},
        )
    )
    second = stamp_event(
        FuturesEvent(
            simulation_id="fsim_test",
            seq=2,
            event_type="ORDER_ACCEPTED",
            occurred_at=cn_dt(date(2026, 9, 17), 10),
            payload={"order_id": "ford_1"},
            previous_event_hash=first.event_hash,
        )
    )
    assert verify_event_chain([first, second]) == second.event_hash

    tampered = second.model_copy(update={"payload": {"order_id": "ford_changed"}})
    with pytest.raises(ValueError, match="event hash mismatch"):
        verify_event_chain([first, tampered])