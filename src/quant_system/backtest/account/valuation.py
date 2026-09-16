"""Ports separating accounting NAV from liquidation-risk valuation."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from quant_system.backtest.account.ledger import AccountLedger


class LiquidatableNAVModel(Protocol):
    def value(self, ledger: AccountLedger) -> Decimal: ...