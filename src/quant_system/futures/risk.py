"""Fail-closed pre-trade controls for futures simulation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_system.futures.account import FuturesAccountLedger
from quant_system.futures.domain import FuturesContract, FuturesOrderIntent, Offset
from quant_system.risk.portfolio import PortfolioRiskControlPlane, RiskContribution
from quant_system.risk.runtime import TradingRuntime


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
    def __init__(
        self,
        runtime: TradingRuntime,
        limits: FuturesRiskLimits | None = None,
        portfolio_risk: PortfolioRiskControlPlane | None = None,
    ) -> None:
        self.runtime = runtime
        self.limits = limits or FuturesRiskLimits()
        self.portfolio_risk = portfolio_risk

    def evaluate(
        self,
        *,
        intent: FuturesOrderIntent,
        contract: FuturesContract,
        ledger: FuturesAccountLedger,
        mark: Decimal,
        marks: dict[str, Decimal],
        active_orders: int,
        margin_call: bool,
        portfolio_contribution: RiskContribution | None = None,
    ) -> RiskDecision:
        if not self.runtime.can_submit(increases_risk=intent.offset == Offset.OPEN):
            reason = (
                "global KillSwitch is engaged"
                if self.runtime.kill_switch.engaged
                else f"global runtime {self.runtime.state} blocks this order"
            )
            return RiskDecision(False, reason)
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
        if self.portfolio_risk is not None:
            if portfolio_contribution is None:
                return RiskDecision(False, "global portfolio contribution is missing")
            self.portfolio_risk.update(portfolio_contribution)
            notional = mark * contract.multiplier * intent.quantity
            net_delta = notional if intent.direction.value == "LONG" else -notional
            global_decision = self.portfolio_risk.evaluate_increment(
                strategy_id=portfolio_contribution.strategy_id,
                gross_delta=notional,
                net_delta=net_delta,
                margin_delta=estimated_margin,
            )
            if not global_decision.approved:
                return RiskDecision(False, "; ".join(global_decision.violations))
        return RiskDecision(True)