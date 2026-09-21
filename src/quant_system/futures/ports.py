"""Ports kept stable when a vn.py/CTP adapter is added."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol

from quant_system.futures.domain import (
    FuturesContract,
    FuturesDailyBar,
    FuturesOrder,
    FuturesOrderIntent,
    FuturesTrade,
)


class FuturesMarketDataProvider(Protocol):
    @property
    def trading_days(self) -> tuple[date, ...]: ...

    def bars_for_day(self, trading_day: date) -> dict[str, FuturesDailyBar]: ...

    def initial_marks(self) -> dict[str, Decimal]: ...


class FuturesGatewayEventSink(Protocol):
    def on_order(self, order: FuturesOrder) -> None: ...

    def on_trade(self, trade: FuturesTrade) -> None: ...

    def on_disconnect(self, reason: str) -> None: ...


class FuturesBrokerGateway(Protocol):
    gateway_name: str

    def connect(self, sink: FuturesGatewayEventSink) -> None: ...

    def disconnect(self) -> None: ...

    def subscribe(self, symbols: list[str]) -> None: ...

    def send_order(self, intent: FuturesOrderIntent) -> str: ...

    def cancel_order(self, order_id: str) -> None: ...

    def query_contracts(self) -> list[FuturesContract]: ...