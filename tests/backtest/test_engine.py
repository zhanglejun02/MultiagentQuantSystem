from __future__ import annotations

import json
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from tests.helpers import ScheduledWeightsStrategy, make_engine_with

from quant_system.backtest.request import BacktestRequest
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.domain.types import cn_dt
from quant_system.strategies import resolve_strategy


def run_backtest(provider, rulebook, store, registry, strategy, start, end, **kw):
    engine = make_engine_with(provider, rulebook, store, registry, strategy)
    request = BacktestRequest(
        strategy_id=getattr(strategy, "strategy_id", "scheduled"),
        dataset_snapshot_id="",
        start_date=start,
        end_date=end,
        initial_cash=Decimal("1000000"),
        **kw,
    )
    return engine.run(request)


def load_events(result, event_types: set[str] | None = None) -> list[dict]:
    rows = [
        json.loads(line)
        for line in (Path(result.run_dir) / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    if event_types:
        rows = [r for r in rows if r["event_type"] in event_types]
    return rows


def load_orders(result) -> list[dict]:
    return [
        json.loads(line)
        for line in (Path(result.run_dir) / "orders.jsonl").read_text().splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
def test_no_same_bar_fill(provider, rulebook, store, registry):
    """A signal from Day T close must never fill on Day T's bar."""
    result = run_backtest(
        provider, rulebook, store, registry,
        resolve_strategy("momentum_topk", {}),
        date(2024, 1, 1), date(2024, 3, 1),
    )
    fills = load_events(result, {"FILL", "PARTIAL_FILL"})
    assert fills, "expected at least one fill in the window"
    order_by_id = {o["order_id"]: o for o in load_orders(result)}
    for f in fills:
        client_id = order_by_id[f["order_id"]]["client_order_id"]
        decision_date = date.fromisoformat(client_id.split(":")[1])
        assert date.fromisoformat(f["trade_date"]) > decision_date


def test_suspended_security_cannot_fill(provider, rulebook, store, registry):
    strategy = ScheduledWeightsStrategy({
        date(2024, 3, 6): {"600003.SSE": Decimal("0.1")},
        date(2024, 3, 8): {"600003.SSE": Decimal("0.3")},
    })
    result = run_backtest(provider, rulebook, store, registry, strategy,
                          date(2024, 3, 5), date(2024, 3, 14))
    fills = load_events(result, {"FILL", "PARTIAL_FILL"})
    suspension = (date(2024, 3, 11), date(2024, 3, 22))
    for f in fills:
        d = date.fromisoformat(f["trade_date"])
        assert not (suspension[0] <= d <= suspension[1] and f["symbol"] == "600003.SSE")
    cancels = load_events(result, {"CANCEL"})
    assert any(c["reason"] == "SUSPENDED" for c in cancels)
    assert any("SUSPENDED" in v for v in result.violations)


def test_limit_up_buy_does_not_fill(provider, rulebook, store, registry):
    strategy = ScheduledWeightsStrategy({date(2024, 3, 4): {"600005.SSE": Decimal("0.3")}})
    result = run_backtest(provider, rulebook, store, registry, strategy,
                          date(2024, 3, 1), date(2024, 3, 6))
    fills = load_events(result, {"FILL", "PARTIAL_FILL"})
    assert not any(
        f["symbol"] == "600005.SSE" and f["trade_date"] == "2024-03-05" for f in fills
    )
    cancels = load_events(result, {"CANCEL"})
    assert any(c["reason"] == "LIMIT_UP_NO_FILL" for c in cancels)


def test_limit_down_sell_does_not_fill(provider, rulebook, store, registry):
    strategy = ScheduledWeightsStrategy({
        date(2024, 3, 28): {"600005.SSE": Decimal("0.3")},
        date(2024, 4, 1): {},
    })
    result = run_backtest(provider, rulebook, store, registry, strategy,
                          date(2024, 3, 27), date(2024, 4, 4))
    fills = load_events(result, {"FILL", "PARTIAL_FILL"})
    assert not any(
        f["symbol"] == "600005.SSE" and f["side"] == "SELL" and f["trade_date"] == "2024-04-02"
        for f in fills
    ), "limit-down day sell must not fill"
    cancels = load_events(result, {"CANCEL"})
    assert any(c["reason"] == "LIMIT_DOWN_NO_FILL" for c in cancels)
    # the sell eventually completes once trading resumes off the limit price
    assert any(
        f["symbol"] == "600005.SSE" and f["side"] == "SELL" and f["trade_date"] > "2024-04-02"
        for f in fills
    )


def test_participation_limit_causes_partial_fill(provider, rulebook, store, registry):
    strategy = ScheduledWeightsStrategy({date(2024, 1, 2): {"000002.SZSE": Decimal("0.3")}})
    result = run_backtest(provider, rulebook, store, registry, strategy,
                          date(2024, 1, 1), date(2024, 1, 10))
    partials = load_events(result, {"PARTIAL_FILL"})
    assert partials, "low-liquidity symbol should partially fill under participation cap"
    assert result.execution_metrics["fill_ratio"] < 1.0
    assert result.execution_metrics["partial_fills"] >= 1
    # every partial respects the 5% participation cap (fixture volume <= 8000 -> <= 400)
    for p in partials:
        assert p["quantity"] <= 400


def test_t_plus_1_buy_then_sell_round_trip(provider, rulebook, store, registry):
    strategy = ScheduledWeightsStrategy({
        date(2024, 1, 2): {"600000.SSE": Decimal("0.3")},
        date(2024, 1, 3): {},
    })
    result = run_backtest(provider, rulebook, store, registry, strategy,
                          date(2024, 1, 1), date(2024, 1, 5))
    fills = load_events(result, {"FILL", "PARTIAL_FILL"})
    buys = [f for f in fills if f["side"] == "BUY"]
    sells = [f for f in fills if f["side"] == "SELL"]
    assert buys and buys[0]["trade_date"] == "2024-01-03"
    assert sells and sells[0]["trade_date"] == "2024-01-04"  # T+1 respected


def test_risk_rejection_recorded(provider, rulebook, store, registry):
    # projected weight 0.5 breaches the 0.35 default limit
    strategy = ScheduledWeightsStrategy({date(2024, 1, 2): {"600000.SSE": Decimal("0.5")}})
    result = run_backtest(provider, rulebook, store, registry, strategy,
                          date(2024, 1, 1), date(2024, 1, 5))
    rejected = load_events(result, {"RISK_REJECTED"})
    assert rejected
    assert any("MaxPositionWeightRule" in v for r in rejected for v in r["violations"])
    assert not load_events(result, {"FILL", "PARTIAL_FILL"})


def test_backtest_artifacts_registered_with_lineage(provider, rulebook, store, registry):
    from quant_system.data.snapshots.service import DataSnapshotService

    snapshots = DataSnapshotService(store, registry)
    snap = snapshots.create_snapshot(provider.data_dir.as_posix(), producer="test")
    strategy = resolve_strategy("momentum_topk", {})
    engine = make_engine_with(provider, rulebook, store, registry, strategy)
    result = engine.run(BacktestRequest(
        strategy_id="momentum_topk", dataset_snapshot_id=snap.artifact_id,
        start_date=date(2024, 1, 1), end_date=date(2024, 2, 15),
        initial_cash=Decimal("1000000"),
    ))
    env = registry.get(result.result_artifact_id)
    assert snap.artifact_id in env.parent_artifact_ids
    assert result.spec_artifact_id in env.parent_artifact_ids
    assert env.content_hash


@pytest.mark.parametrize("nav_positive", [True])
def test_engine_conserves_value_and_invariants(provider, rulebook, store, registry, nav_positive):
    result = run_backtest(
        provider, rulebook, store, registry, resolve_strategy("momentum_topk", {}),
        date(2024, 1, 1), date(2024, 6, 28),
    )
    equity = (Path(result.run_dir) / "equity_curve.csv").read_text().splitlines()[1:]
    for line in equity:
        _, cash, frozen, mv, nav = line.split(",")
        assert Decimal(cash) >= 0
        assert Decimal(frozen) >= 0
        assert Decimal(nav) == Decimal(cash) + Decimal(frozen) + Decimal(mv)


def test_engine_applies_split_before_t_plus_one_settlement(
    fixture_dir, tmp_path, rulebook, store, registry
):
    data_dir = tmp_path / "corporate-action-fixture"
    shutil.copytree(fixture_dir, data_dir)
    (data_dir / "corporate_actions.json").write_text(json.dumps([{
        "symbol": "600000.SSE",
        "kind": "STOCK_SPLIT",
        "ex_date": "2024-01-04",
        "split_ratio": "2",
        "available_at": cn_dt(date(2024, 1, 3), 17, 0).isoformat(),
    }]))
    local_provider = LocalParquetProvider(str(data_dir))
    strategy = ScheduledWeightsStrategy({date(2024, 1, 2): {"600000.SSE": Decimal("0.1")}})
    result = run_backtest(
        local_provider, rulebook, store, registry, strategy,
        date(2024, 1, 2), date(2024, 1, 5),
    )
    events = load_events(result)
    buy = next(
        event for event in events
        if event["event_type"] in {"FILL", "PARTIAL_FILL"}
        and event["trade_date"] == "2024-01-03"
    )
    split = next(event for event in events if event["event_type"] == "CORPORATE_ACTION")
    account = next(
        event for event in events
        if event["event_type"] == "ACCOUNT" and event["trade_date"] == "2024-01-04"
    )
    position = account["positions"]["600000.SSE"]
    assert split["seq"] < account["seq"]
    assert position["total"] == buy["quantity"] * 2
    assert position["pending_settle"] == 0
    assert position["available"] == position["total"]


def test_transaction_cost_stress_is_configured_and_reported(
    provider, rulebook, store, registry
):
    strategy = ScheduledWeightsStrategy({date(2024, 1, 2): {"600000.SSE": Decimal("0.1")}})
    result = run_backtest(
        provider, rulebook, store, registry, strategy,
        date(2024, 1, 2), date(2024, 1, 10),
    )
    sensitivity = result.performance_metrics["transaction_cost_sensitivity"]
    assert set(sensitivity) == {"1", "1.5", "2", "3"}
    assert sensitivity["3"]["estimated_final_nav"] < sensitivity["1"]["estimated_final_nav"]


def test_t_plus_one_afternoon_volume_cannot_change_open_fill(
    fixture_dir, rulebook, store, registry
):
    normal_provider = LocalParquetProvider(fixture_dir)
    altered_provider = LocalParquetProvider(fixture_dir)
    altered_provider._bars = [
        bar.model_copy(update={"volume": bar.volume * 1000})
        if bar.symbol == "600000.SSE" and bar.trade_date == date(2024, 1, 3)
        else bar
        for bar in altered_provider._load_bars()
    ]
    strategy = ScheduledWeightsStrategy({date(2024, 1, 2): {"600000.SSE": Decimal("0.1")}})
    normal = run_backtest(
        normal_provider, rulebook, store, registry, strategy,
        date(2024, 1, 2), date(2024, 1, 4),
    )
    altered = run_backtest(
        altered_provider, rulebook, store, registry, strategy,
        date(2024, 1, 2), date(2024, 1, 4),
    )

    def execution_signature(result):
        return [
            (event["trade_date"], event["symbol"], event["side"], event["quantity"], event["price"])
            for event in load_events(result, {"FILL", "PARTIAL_FILL"})
        ]

    assert execution_signature(normal) == execution_signature(altered)


def test_authoritative_cost_stress_rerun_reduces_nav(
    provider, rulebook, store, registry
):
    strategy = ScheduledWeightsStrategy({
        date(2024, 1, 2): {"600000.SSE": Decimal("0.1")},
        date(2024, 1, 4): {},
    })
    base = run_backtest(
        provider, rulebook, store, registry, strategy,
        date(2024, 1, 2), date(2024, 1, 8),
        transaction_cost_multiplier=Decimal("1"),
    )
    stressed = run_backtest(
        provider, rulebook, store, registry, strategy,
        date(2024, 1, 2), date(2024, 1, 8),
        transaction_cost_multiplier=Decimal("3"),
    )
    assert stressed.performance_metrics["final_nav"] < base.performance_metrics["final_nav"]
    assert stressed.execution_metrics["transaction_cost"] > base.execution_metrics["transaction_cost"]
