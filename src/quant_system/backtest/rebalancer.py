"""PortfolioRebalancer: target weights -> lot-aligned order intents.

Sizing uses decision-time closes (legitimately known at T close); execution
happens next trading day.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_DOWN, Decimal

from quant_system.backtest.account.ledger import AccountLedger
from quant_system.domain.enums import OrderSide, OrderType
from quant_system.domain.models import OrderIntent
from quant_system.domain.types import ZERO


class PortfolioRebalancer:
    def __init__(self, lot_size: int = 100, min_order_value: Decimal = Decimal("1000")) -> None:
        self.lot_size = lot_size
        self.min_order_value = min_order_value

    def build_intents(
        self,
        *,
        run_id: str,
        strategy_id: str,
        target_id: str,
        account_id: str,
        trade_date: date,
        weights: dict[str, Decimal],
        closes: dict[str, Decimal],
        ledger: AccountLedger,
        signal_id: str | None = None,
    ) -> list[OrderIntent]:
        nav = ledger.nav
        intents: list[OrderIntent] = []
        symbols = set(weights) | {s for s, p in ledger.positions.items() if p.total > 0}
        for symbol in sorted(symbols):
            close = closes.get(symbol)
            if close is None or close <= 0:
                continue  # no tradable reference price (e.g. suspended)
            target_value = weights.get(symbol, ZERO) * nav
            pos = ledger.positions.get(symbol)
            current_qty = pos.total if pos else 0
            current_value = close * current_qty
            delta_value = target_value - current_value
            raw_qty = (delta_value / close).to_integral_value(rounding=ROUND_DOWN)
            qty = int(raw_qty)
            lot_qty = (abs(qty) // self.lot_size) * self.lot_size
            side = OrderSide.BUY if qty > 0 else OrderSide.SELL
            if side == OrderSide.SELL:
                # Orders execute next trading day: T+1 pending settle will have been
                # released by then, so sellable = total - shares locked by open sells.
                sellable = (pos.total - pos.frozen_for_sell) if pos else 0
                if weights.get(symbol, ZERO) == 0:
                    lot_qty = min(current_qty, abs(qty))  # full close-out may break lots
                lot_qty = min(lot_qty, sellable)
            if lot_qty <= 0 or close * lot_qty < self.min_order_value:
                continue
            intents.append(
                OrderIntent(
                    client_order_id=f"{run_id}:{trade_date.isoformat()}:{symbol}:{side.value}",
                    strategy_id=strategy_id,
                    target_id=target_id,
                    account_id=account_id,
                    symbol=symbol,
                    side=side,
                    order_type=OrderType.MARKET,
                    quantity=lot_qty,
                    reference_price=close,
                    signal_id=signal_id,
                )
            )
        # sells first so that freed cash can fund buys the same execution day
        intents.sort(key=lambda i: (i.side != OrderSide.SELL, i.symbol))
        return intents
