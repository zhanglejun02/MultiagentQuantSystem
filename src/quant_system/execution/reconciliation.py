"""ReconciliationService: broker truth vs internal ledger/OMS.

Any critical mismatch places the TradingRuntime into SAFE_MODE (fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_system.backtest.account.ledger import AccountLedger
from quant_system.domain.enums import MismatchKind
from quant_system.execution.gateway import BrokerGateway
from quant_system.execution.oms import OrderManagementSystem
from quant_system.risk.runtime import TradingRuntime

CASH_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class Mismatch:
    kind: MismatchKind
    detail: str
    critical: bool = True


class ReconciliationService:
    def __init__(
        self,
        broker: BrokerGateway,
        ledger: AccountLedger,
        oms: OrderManagementSystem,
        runtime: TradingRuntime,
    ) -> None:
        self._broker = broker
        self._ledger = ledger
        self._oms = oms
        self._runtime = runtime

    def reconcile(self) -> list[Mismatch]:
        mismatches: list[Mismatch] = []

        broker_cash = self._broker.account_cash()
        internal_cash = self._ledger.cash + self._ledger.frozen_cash
        if abs(broker_cash - internal_cash) > CASH_TOLERANCE:
            mismatches.append(
                Mismatch(
                    MismatchKind.CASH_MISMATCH,
                    f"broker cash {broker_cash} != internal {internal_cash}",
                )
            )

        broker_pos = self._broker.account_positions()
        internal_pos = {s: p.total for s, p in self._ledger.positions.items() if p.total != 0}
        for symbol, qty in broker_pos.items():
            if symbol not in internal_pos:
                mismatches.append(
                    Mismatch(MismatchKind.UNKNOWN_POSITION, f"broker holds unknown {symbol} x{qty}")
                )
            elif internal_pos[symbol] != qty:
                mismatches.append(
                    Mismatch(
                        MismatchKind.POSITION_MISMATCH,
                        f"{symbol}: broker {qty} != internal {internal_pos[symbol]}",
                    )
                )
        for symbol, qty in internal_pos.items():
            if symbol not in broker_pos:
                mismatches.append(
                    Mismatch(
                        MismatchKind.POSITION_MISMATCH,
                        f"{symbol}: internal {qty} missing at broker",
                    )
                )

        internal_order_ids = {o.order_id for o in self._oms.all_orders()}
        for oid in self._broker.known_orders():
            if oid not in internal_order_ids:
                mismatches.append(
                    Mismatch(MismatchKind.UNKNOWN_ORDER, f"broker has unknown order {oid}")
                )

        broker_trades = len(self._broker.trades())
        internal_trades = len(self._ledger.fills)
        if broker_trades != internal_trades:
            mismatches.append(
                Mismatch(
                    MismatchKind.TRADE_MISMATCH,
                    f"broker trades {broker_trades} != internal {internal_trades}",
                )
            )

        if any(m.critical for m in mismatches):
            self._runtime.enter_safe_mode(
                "; ".join(f"{m.kind}: {m.detail}" for m in mismatches)
            )
        return mismatches
