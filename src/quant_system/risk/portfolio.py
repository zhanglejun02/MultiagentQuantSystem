"""Asset-agnostic portfolio risk and capital allocation control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from quant_system.domain.types import ZERO


@dataclass(frozen=True)
class RiskContribution:
    account_id: str
    strategy_id: str
    asset_class: str
    nav: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    margin: Decimal = ZERO


@dataclass(frozen=True)
class PortfolioRiskLimits:
    max_gross_leverage: Decimal = Decimal("1.00")
    max_abs_net_exposure: Decimal = Decimal("1.00")
    max_margin_ratio: Decimal = Decimal("0.50")
    max_strategy_allocation: Decimal = Decimal("1.00")


@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    total_nav: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    margin: Decimal
    strategy_gross_exposure: dict[str, Decimal]
    asset_class_gross_exposure: dict[str, Decimal]


@dataclass(frozen=True)
class PortfolioRiskDecision:
    approved: bool
    violations: tuple[str, ...] = ()


@dataclass
class CapitalAllocator:
    default_limit: Decimal = Decimal("1.00")
    strategy_limits: dict[str, Decimal] = field(default_factory=dict)

    def limit_for(self, strategy_id: str) -> Decimal:
        return self.strategy_limits.get(strategy_id, self.default_limit)


class PortfolioRiskControlPlane:
    def __init__(
        self,
        limits: PortfolioRiskLimits | None = None,
        allocator: CapitalAllocator | None = None,
    ) -> None:
        self.limits = limits or PortfolioRiskLimits()
        self.allocator = allocator or CapitalAllocator(
            default_limit=self.limits.max_strategy_allocation
        )
        self._contributions: dict[tuple[str, str, str], RiskContribution] = {}
        self._account_nav: dict[str, Decimal] = {}

    def update(self, contribution: RiskContribution) -> None:
        if contribution.nav < 0:
            raise ValueError("NAV cannot be negative")
        if contribution.gross_exposure < 0 or contribution.margin < 0:
            raise ValueError("gross exposure and margin cannot be negative")
        self._account_nav[contribution.account_id] = contribution.nav
        key = (contribution.account_id, contribution.strategy_id, contribution.asset_class)
        self._contributions[key] = contribution

    def snapshot(self) -> PortfolioRiskSnapshot:
        strategy_gross: dict[str, Decimal] = {}
        asset_gross: dict[str, Decimal] = {}
        for item in self._contributions.values():
            strategy_gross[item.strategy_id] = (
                strategy_gross.get(item.strategy_id, ZERO) + item.gross_exposure
            )
            asset_gross[item.asset_class] = (
                asset_gross.get(item.asset_class, ZERO) + item.gross_exposure
            )
        return PortfolioRiskSnapshot(
            total_nav=sum(self._account_nav.values(), ZERO),
            gross_exposure=sum(
                (item.gross_exposure for item in self._contributions.values()), ZERO
            ),
            net_exposure=sum(
                (item.net_exposure for item in self._contributions.values()), ZERO
            ),
            margin=sum((item.margin for item in self._contributions.values()), ZERO),
            strategy_gross_exposure=strategy_gross,
            asset_class_gross_exposure=asset_gross,
        )

    def evaluate_increment(
        self,
        *,
        strategy_id: str,
        gross_delta: Decimal,
        net_delta: Decimal,
        margin_delta: Decimal = ZERO,
    ) -> PortfolioRiskDecision:
        snapshot = self.snapshot()
        if snapshot.total_nav <= 0:
            return PortfolioRiskDecision(False, ("global NAV is not positive",))
        projected_gross = snapshot.gross_exposure + gross_delta
        projected_net = snapshot.net_exposure + net_delta
        projected_margin = snapshot.margin + margin_delta
        strategy_gross = snapshot.strategy_gross_exposure.get(strategy_id, ZERO) + gross_delta
        violations: list[str] = []
        if projected_gross / snapshot.total_nav > self.limits.max_gross_leverage:
            violations.append("global gross leverage exceeded")
        if abs(projected_net) / snapshot.total_nav > self.limits.max_abs_net_exposure:
            violations.append("global net exposure exceeded")
        if projected_margin / snapshot.total_nav > self.limits.max_margin_ratio:
            violations.append("global margin ratio exceeded")
        if strategy_gross / snapshot.total_nav > self.allocator.limit_for(strategy_id):
            violations.append(f"strategy {strategy_id} capital allocation exceeded")
        return PortfolioRiskDecision(not violations, tuple(violations))