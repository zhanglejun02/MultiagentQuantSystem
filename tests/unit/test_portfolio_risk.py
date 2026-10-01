from decimal import Decimal

from quant_system.risk.portfolio import (
    CapitalAllocator,
    PortfolioRiskControlPlane,
    PortfolioRiskLimits,
    RiskContribution,
)


def contribution(
    account_id: str,
    strategy_id: str,
    asset_class: str,
    gross: str,
) -> RiskContribution:
    return RiskContribution(
        account_id=account_id,
        strategy_id=strategy_id,
        asset_class=asset_class,
        nav=Decimal("50"),
        gross_exposure=Decimal(gross),
        net_exposure=Decimal(gross),
    )


def test_cross_asset_exposure_is_rejected_at_portfolio_level():
    plane = PortfolioRiskControlPlane(
        PortfolioRiskLimits(
            max_gross_leverage=Decimal("1.00"),
            max_abs_net_exposure=Decimal("2.00"),
            max_strategy_allocation=Decimal("1.00"),
        )
    )
    plane.update(contribution("stock", "stock-alpha", "EQUITY", "50"))
    plane.update(contribution("futures", "futures-alpha", "FUTURES", "50"))

    decision = plane.evaluate_increment(
        strategy_id="futures-alpha",
        gross_delta=Decimal("10"),
        net_delta=Decimal("-10"),
    )

    assert decision.approved is False
    assert "global gross leverage exceeded" in decision.violations


def test_capital_allocator_enforces_strategy_specific_budget():
    plane = PortfolioRiskControlPlane(
        PortfolioRiskLimits(max_gross_leverage=Decimal("2.00")),
        CapitalAllocator(strategy_limits={"crowded-alpha": Decimal("0.20")}),
    )
    plane.update(contribution("stock", "baseline", "EQUITY", "10"))
    plane.update(contribution("futures", "crowded-alpha", "FUTURES", "15"))

    decision = plane.evaluate_increment(
        strategy_id="crowded-alpha",
        gross_delta=Decimal("10"),
        net_delta=Decimal("10"),
    )

    assert decision.approved is False
    assert decision.violations == ("strategy crowded-alpha capital allocation exceeded",)


def test_snapshot_aggregates_asset_classes_and_strategies():
    plane = PortfolioRiskControlPlane()
    plane.update(contribution("stock", "shared", "EQUITY", "20"))
    plane.update(contribution("futures", "shared", "FUTURES", "15"))

    snapshot = plane.snapshot()

    assert snapshot.total_nav == Decimal("100")
    assert snapshot.strategy_gross_exposure == {"shared": Decimal("35")}
    assert snapshot.asset_class_gross_exposure == {
        "EQUITY": Decimal("20"),
        "FUTURES": Decimal("15"),
    }


def test_same_account_strategies_do_not_overwrite_or_duplicate_nav():
    plane = PortfolioRiskControlPlane()
    plane.update(contribution("shared-account", "alpha-a", "EQUITY", "20"))
    plane.update(contribution("shared-account", "alpha-b", "EQUITY", "15"))

    snapshot = plane.snapshot()

    assert snapshot.total_nav == Decimal("50")
    assert snapshot.gross_exposure == Decimal("35")
    assert snapshot.strategy_gross_exposure == {
        "alpha-a": Decimal("20"),
        "alpha-b": Decimal("15"),
    }


def test_account_nav_can_refresh_between_risk_checks():
    plane = PortfolioRiskControlPlane()
    plane.update(contribution("stock", "alpha", "EQUITY", "20"))
    plane.update(
        RiskContribution(
            account_id="stock",
            strategy_id="alpha",
            asset_class="EQUITY",
            nav=Decimal("55"),
            gross_exposure=Decimal("22"),
            net_exposure=Decimal("22"),
        )
    )

    assert plane.snapshot().total_nav == Decimal("55")