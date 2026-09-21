"""Fail-closed pre-trade controls for futures simulation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_system.futures.account import FuturesAccountLedger
from quant_system.futures.domain import FuturesContract, FuturesOrderIntent, Offset


@dataclass(frozen=True)
class FuturesRiskLimits:
    max_order_lots: int = 20
    max_contract_lots: int = 50
    max_active_orders: int = 20
    max_margin_ratio: Decimal = Decimal("0.70")
    max_price_deviation: Decimal = Decimal("0.08")


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str = ""


class FuturesPreTradeRisk:
    def __init__(self, limits: FuturesRiskLimits | None = None) -> None:
        self.limits = limits or FuturesRiskLimits()

    def evaluate(
        self,
        *,
        intent: FuturesOrderIntent,
        contract: FuturesContract,
        ledger: FuturesAccountLedger,
        mark: Decimal,
        marks: dict[str, Decimal],
        active_orders: int,
        kill_switch_engaged: bool,
        margin_call: bool,
    ) -> RiskDecision:
        if kill_switch_engaged:
            return RiskDecision(False, "global KillSwitch is engaged")
        if margin_call and intent.offset == Offset.OPEN:
            return RiskDecision(False, "account is in MARGIN_CALL; only closing is allowed")
        if intent.quantity > self.limits.max_order_lots:
            return RiskDecision(False, "max_order_lots exceeded")
        if active_orders >= self.limits.max_active_orders:
            return RiskDecision(False, "max_active_orders exceeded")
        if intent.limit_price is not None:
            deviation = abs(intent.limit_price / mark - Decimal("1"))
            if deviation > self.limits.max_price_deviation:
                return RiskDecision(False, "limit price deviation exceeded")
        if intent.offset != Offset.OPEN:
            closable = ledger.closable_quantity(intent.symbol, intent.direction, intent.offset)
            if intent.quantity > closable:
                return RiskDecision(False, "insufficient closable position")
            return RiskDecision(True)

        current = ledger.total_quantity(intent.symbol)
        if current + intent.quantity > self.limits.max_contract_lots:
            return RiskDecision(False, "max_contract_lots exceeded")
        snapshot = ledger.snapshot(marks)
        estimated_margin = (
            mark * contract.multiplier * contract.margin_rate(intent.direction) * intent.quantity
        )
        estimated_fee = contract.fee(intent.offset, intent.quantity)
        if estimated_margin + estimated_fee > snapshot.available:
            return RiskDecision(False, "insufficient available funds")
        projected_ratio = (snapshot.margin + estimated_margin) / snapshot.equity
        if projected_ratio > self.limits.max_margin_ratio:
            return RiskDecision(False, "max_margin_ratio exceeded")
        return RiskDecision(True)