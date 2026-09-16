"""Pre-trade risk engine with plugin rules. Sits before every order
submission; unknown errors fail closed (reject)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

from quant_system.backtest.account.ledger import AccountLedger
from quant_system.domain.enums import OrderSide
from quant_system.domain.models import OrderIntent
from quant_system.domain.types import ZERO
from quant_system.risk.runtime import KillSwitch


@dataclass
class RiskContext:
    ledger: AccountLedger
    trade_date: date
    last_close: dict[str, Decimal]
    market_data_date: date
    active_order_count: int = 0
    today_submitted_value: Decimal = ZERO
    seen_client_order_ids: set[str] = field(default_factory=set)
    fee_buffer: Decimal = Decimal("1.01")


@dataclass(frozen=True)
class RiskViolation:
    rule: str
    message: str


class PreTradeRule(Protocol):
    name: str

    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskViolation | None: ...


def _intent_value(intent: OrderIntent) -> Decimal:
    price = intent.limit_price if intent.limit_price is not None else intent.reference_price
    return price * intent.quantity


class MaxOrderValueRule:
    name = "MaxOrderValueRule"

    def __init__(self, max_order_value: Decimal) -> None:
        self.max_order_value = max_order_value

    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskViolation | None:
        value = _intent_value(intent)
        if value > self.max_order_value:
            return RiskViolation(self.name, f"order value {value} > limit {self.max_order_value}")
        return None


class MaxPositionWeightRule:
    name = "MaxPositionWeightRule"

    def __init__(self, max_weight: Decimal) -> None:
        self.max_weight = max_weight

    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskViolation | None:
        if intent.side != OrderSide.BUY:
            return None
        nav = ctx.ledger.nav
        if nav <= 0:
            return RiskViolation(self.name, "non-positive NAV")
        pos = ctx.ledger.positions.get(intent.symbol)
        current_value = pos.market_value if pos else ZERO
        projected = (current_value + _intent_value(intent)) / nav
        if projected > self.max_weight:
            return RiskViolation(
                self.name, f"projected weight {projected:.4f} > limit {self.max_weight}"
            )
        return None


class MaxDailyTurnoverRule:
    name = "MaxDailyTurnoverRule"

    def __init__(self, max_turnover: Decimal) -> None:
        self.max_turnover = max_turnover

    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskViolation | None:
        nav = ctx.ledger.nav
        if nav <= 0:
            return RiskViolation(self.name, "non-positive NAV")
        projected = (ctx.today_submitted_value + _intent_value(intent)) / nav
        if projected > self.max_turnover:
            return RiskViolation(self.name, f"daily turnover {projected:.4f} > {self.max_turnover}")
        return None


class MaxActiveOrdersRule:
    name = "MaxActiveOrdersRule"

    def __init__(self, max_active: int) -> None:
        self.max_active = max_active

    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskViolation | None:
        if ctx.active_order_count >= self.max_active:
            return RiskViolation(self.name, f"active orders {ctx.active_order_count} >= {self.max_active}")
        return None


class DuplicateOrderRule:
    name = "DuplicateOrderRule"

    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskViolation | None:
        if intent.client_order_id in ctx.seen_client_order_ids:
            return RiskViolation(self.name, f"duplicate client_order_id {intent.client_order_id}")
        return None


class PriceDeviationRule:
    name = "PriceDeviationRule"

    def __init__(self, max_deviation: Decimal) -> None:
        self.max_deviation = max_deviation

    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskViolation | None:
        if intent.limit_price is None:
            return None
        last = ctx.last_close.get(intent.symbol)
        if last is None or last == 0:
            return RiskViolation(self.name, f"no reference close for {intent.symbol}")
        deviation = abs(intent.limit_price - last) / last
        if deviation > self.max_deviation:
            return RiskViolation(self.name, f"price deviation {deviation:.4f} > {self.max_deviation}")
        return None


class StaleMarketDataRule:
    name = "StaleMarketDataRule"

    def __init__(self, max_staleness_days: int) -> None:
        self.max_staleness_days = max_staleness_days

    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskViolation | None:
        age = (ctx.trade_date - ctx.market_data_date).days
        if age > self.max_staleness_days:
            return RiskViolation(self.name, f"market data {age}d old > {self.max_staleness_days}d")
        return None


class AvailableCashRule:
    name = "AvailableCashRule"

    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskViolation | None:
        if intent.side != OrderSide.BUY:
            return None
        est_cost = _intent_value(intent) * ctx.fee_buffer
        if est_cost > ctx.ledger.cash:
            return RiskViolation(
                self.name, f"estimated cost {est_cost:.2f} > cash {ctx.ledger.cash:.2f}"
            )
        return None


class AvailablePositionRule:
    name = "AvailablePositionRule"

    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskViolation | None:
        if intent.side != OrderSide.SELL:
            return None
        pos = ctx.ledger.positions.get(intent.symbol)
        # Execution happens next trading day: pending T+1 settle will be released,
        # so sellable is total minus shares already locked by open sell orders.
        sellable = (pos.total - pos.frozen_for_sell) if pos else 0
        if intent.quantity > sellable:
            return RiskViolation(
                self.name, f"sell {intent.quantity} > sellable {sellable} (T+1)"
            )
        return None


class PreTradeRiskEngine:
    def __init__(self, rules: list[PreTradeRule], kill_switch: KillSwitch) -> None:
        self._rules = rules
        self._kill_switch = kill_switch

    def evaluate(self, intent: OrderIntent, ctx: RiskContext) -> list[RiskViolation]:
        if self._kill_switch.engaged:
            return [RiskViolation("KillSwitch", f"kill switch engaged: {self._kill_switch.reason}")]
        violations: list[RiskViolation] = []
        for rule in self._rules:
            try:
                v = rule.check(intent, ctx)
            except Exception as exc:  # fail closed
                v = RiskViolation(rule.name, f"rule error, rejecting: {type(exc).__name__}: {exc}")
            if v is not None:
                violations.append(v)
        return violations
