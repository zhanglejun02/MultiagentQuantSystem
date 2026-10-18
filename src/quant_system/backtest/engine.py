"""EventDrivenBacktestEngine: the authoritative engine.

Execution semantics (enforced, not optional):
    Day T close -> data finalized -> signal -> target
    Day T+1     -> orders -> pre-trade risk -> exchange simulation -> fills -> ledger

A signal generated from Day T close can never fill on Day T.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

from quant_system.artifacts.envelope import hash_json
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.artifacts.types import backtest_result_artifact, backtest_spec_artifact
from quant_system.backtest.account.ledger import AccountLedger, CorporateAction
from quant_system.backtest.clock import HistoricalClock
from quant_system.backtest.event_store import EventStore
from quant_system.backtest.events import (
    AccountEvent,
    CancelEvent,
    CorporateActionEvent,
    FillEvent,
    MarketEvent,
    OrderAcceptedEvent,
    OrderIntentEvent,
    OrderSubmittedEvent,
    PartialFillEvent,
    RiskApprovedEvent,
    RiskRejectedEvent,
    SignalEvent,
    TargetEvent,
)
from quant_system.backtest.exchange import ExchangeSimulator
from quant_system.backtest.feed import MarketDataFeed
from quant_system.backtest.fees import CashReservationPolicy, ConfigurableFeeModel
from quant_system.backtest.fill import NextOpenFillModel, VolumeParticipationFillModel
from quant_system.backtest.metrics import (
    compute_execution_metrics,
    compute_performance_metrics,
    compute_prediction_ic,
)
from quant_system.backtest.rebalancer import PortfolioRebalancer
from quant_system.backtest.request import BacktestRequest, BacktestResult
from quant_system.backtest.slippage import FixedBpsSlippageModel
from quant_system.backtest.strategy import Strategy, StrategyContext
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.data.snapshots.service import DataSnapshotService
from quant_system.data.universe.service import UniverseService
from quant_system.domain.enums import OrderSide, OrderStatus
from quant_system.domain.exceptions import (
    InsufficientCashError,
    MarketRuleViolationError,
    QuantSystemError,
)
from quant_system.domain.models import FillRecord
from quant_system.domain.types import ZERO, cn_dt, new_id, to_decimal
from quant_system.execution.gateway import SafetyGatedBrokerGateway
from quant_system.execution.oms import Order, OrderManagementSystem
from quant_system.execution.paper import PaperBrokerGateway
from quant_system.risk.portfolio import (
    CapitalAllocator,
    PortfolioRiskControlPlane,
    PortfolioRiskLimits,
)
from quant_system.risk.pretrade import (
    AvailableCashRule,
    AvailablePositionRule,
    DuplicateOrderRule,
    GlobalPortfolioRiskRule,
    MaxActiveOrdersRule,
    MaxDailyTurnoverRule,
    MaxOrderValueRule,
    MaxPositionWeightRule,
    PreTradeRiskEngine,
    PriceDeviationRule,
    RiskContext,
    StaleMarketDataRule,
)
from quant_system.risk.runtime import KillSwitch, TradingRuntime
from quant_system.rules.reference_price import CorporateActionReferencePriceService
from quant_system.rules.rulebook import MarketRuleBook, TradingRules

StrategyResolver = Callable[[str, dict], Strategy]

def default_risk_rules(
    cfg: dict,
    portfolio_risk: PortfolioRiskControlPlane | None = None,
) -> list:
    rules = [
        MaxOrderValueRule(to_decimal(cfg.get("max_order_value", "2000000"))),
        MaxPositionWeightRule(to_decimal(cfg.get("max_position_weight", "0.35"))),
        MaxDailyTurnoverRule(to_decimal(cfg.get("max_daily_turnover", "1.0"))),
        MaxActiveOrdersRule(int(cfg.get("max_active_orders", 50))),
        DuplicateOrderRule(),
        PriceDeviationRule(to_decimal(cfg.get("max_price_deviation", "0.08"))),
        StaleMarketDataRule(int(cfg.get("max_market_data_staleness_days", 3))),
        AvailableCashRule(),
        AvailablePositionRule(),
    ]
    if portfolio_risk is not None:
        rules.append(GlobalPortfolioRiskRule(portfolio_risk))
    return rules


class EventDrivenBacktestEngine:
    def __init__(
        self,
        *,
        provider: LocalParquetProvider,
        rulebook: MarketRuleBook,
        store: LocalArtifactStore,
        registry: ArtifactRegistry,
        strategy_resolver: StrategyResolver,
        risk_config: dict | None = None,
        producer: str = "EventDrivenBacktestEngine",
        code_commit: str = "unknown",
    ) -> None:
        self._provider = provider
        self._rulebook = rulebook
        self._store = store
        self._registry = registry
        self._resolve_strategy = strategy_resolver
        self._risk_config = risk_config or {}
        self._producer = producer
        self._code_commit = code_commit

    # ------------------------------------------------------------------
    def run(self, request: BacktestRequest) -> BacktestResult:
        if request.dataset_snapshot_id:
            path = DataSnapshotService(self._store, self._registry).resolve_path(
                request.dataset_snapshot_id
            )
            frozen_engine = EventDrivenBacktestEngine(
                provider=LocalParquetProvider(str(path)),
                rulebook=self._rulebook,
                store=self._store,
                registry=self._registry,
                strategy_resolver=self._resolve_strategy,
                risk_config=self._risk_config,
                producer=self._producer,
                code_commit=self._code_commit,
            )
            return frozen_engine._run(request)
        return self._run(request)

    def _run(self, request: BacktestRequest) -> BacktestResult:
        if request.rulebook_version != self._rulebook.rulebook_version:
            raise MarketRuleViolationError(
                f"requested rulebook {request.rulebook_version} != loaded "
                f"{self._rulebook.rulebook_version}"
            )
        run_id = new_id("bt_")
        days = self._provider.trading_calendar(request.start_date, request.end_date)
        if not days:
            raise QuantSystemError("no trading days in requested window")
        if request.benchmark:
            self._provider.benchmark_series(request.benchmark, days)
        clock = HistoricalClock(days)
        feed = MarketDataFeed(self._provider)
        universe_svc = UniverseService(self._provider)
        ledger = AccountLedger(f"paper:{run_id}", request.initial_cash)
        kill_switch = KillSwitch()
        runtime = TradingRuntime(kill_switch)
        pretrade_config = self._risk_config.get("pretrade", self._risk_config)
        portfolio_config = self._risk_config.get("portfolio", {})
        portfolio_risk = PortfolioRiskControlPlane(
            PortfolioRiskLimits(
                max_gross_leverage=to_decimal(
                    portfolio_config.get("max_gross_leverage", "1.00")
                ),
                max_abs_net_exposure=to_decimal(
                    portfolio_config.get("max_abs_net_exposure", "1.00")
                ),
                max_margin_ratio=to_decimal(
                    portfolio_config.get("max_margin_ratio", "0.50")
                ),
                max_strategy_allocation=to_decimal(
                    portfolio_config.get("max_strategy_allocation", "1.00")
                ),
            ),
            CapitalAllocator(
                default_limit=to_decimal(
                    portfolio_config.get("max_strategy_allocation", "1.00")
                )
            ),
        )
        broker = SafetyGatedBrokerGateway(PaperBrokerGateway(request.initial_cash), runtime)
        oms = OrderManagementSystem(runtime)
        risk_engine = PreTradeRiskEngine(
            default_risk_rules(pretrade_config, portfolio_risk), runtime
        )
        slippage = FixedBpsSlippageModel(
            to_decimal(request.slippage_model.get("bps", "5"))
            * request.transaction_cost_multiplier
        )
        fill_model = VolumeParticipationFillModel(
            NextOpenFillModel(slippage), request.participation_limit
        )
        exchange = ExchangeSimulator(fill_model)
        fee_model = ConfigurableFeeModel(request.transaction_cost_multiplier)
        reservation_policy = CashReservationPolicy(request.cash_reservation_multiplier)
        reference_prices = CorporateActionReferencePriceService()
        rebalancer = PortfolioRebalancer()
        strategy = self._resolve_strategy(request.strategy_id, dict(request.config))
        events = EventStore()

        pending: list[tuple[Order, int]] = []
        last_close: dict[str, Decimal] = {}
        last_data_date: date = days[0]
        nav_series: list[tuple[date, Decimal]] = []
        closes_by_day: dict[date, dict[str, float]] = {}
        opens_by_day: dict[date, dict[str, float]] = {}
        scores_by_day: dict[date, dict[str, float]] = {}
        violations: list[str] = []
        seen_client_ids: set[str] = set()
        ordered_qty = 0
        filled_qty = 0
        traded_value = ZERO
        n_rejects = 0
        n_partials = 0

        for day in days:
            as_of_open = cn_dt(day, 9, 15)
            day_actions = self._provider.get_corporate_actions(day, day, as_of=as_of_open)
            actions_by_symbol: dict[str, list] = {}
            for action in day_actions:
                actions_by_symbol.setdefault(action.symbol, []).append(action)
                ledger.apply_corporate_action(
                    CorporateAction(
                        symbol=action.symbol,
                        kind=action.kind,
                        ex_date=action.ex_date,
                        cash_per_share=action.cash_per_share,
                        split_ratio=action.split_ratio,
                    )
                )
                events.append(
                    CorporateActionEvent(
                        ts=cn_dt(day, 9, 0), trade_date=day, symbol=action.symbol,
                        kind=action.kind, cash_per_share=action.cash_per_share,
                        split_ratio=action.split_ratio,
                    )
                )
            ledger.start_of_day(day)
            bars = feed.market_bars(day)
            events.append(
                MarketEvent(ts=clock.session_open(day), trade_date=day,
                            kind="SESSION_OPEN", n_bars=len(bars))
            )

            # --- T+1 open: execute orders decided at previous close ----------
            for order, liquidity_volume in pending:
                split_actions = [
                    action for action in actions_by_symbol.get(order.symbol, [])
                    if action.kind in {"STOCK_SPLIT", "BONUS_SHARE", "SPLIT"}
                ]
                if split_actions:
                    self._cancel(
                        order, day, clock, ledger, events,
                        "CORPORATE_ACTION_ORDER_INVALIDATED",
                    )
                    continue
                self._execute_order(
                    order=order, day=day, bars=bars, clock=clock, exchange=exchange,
                    fee_model=fee_model, universe_svc=universe_svc, ledger=ledger,
                    broker=broker, events=events, violations=violations,
                    liquidity_volume=liquidity_volume,
                    corporate_actions=actions_by_symbol.get(order.symbol, []),
                    reference_prices=reference_prices,
                )
                if order.filled_quantity > 0:
                    filled_qty += order.filled_quantity
                    traded_value += order.avg_fill_price * order.filled_quantity
                if order.status == OrderStatus.CANCELLED and order.filled_quantity > 0:
                    n_partials += 1
            pending = []

            # --- mark to market at close --------------------------------------
            closes = {s: b.close for s, b in bars.items()}
            opens_by_day[day] = {s: float(b.open) for s, b in bars.items()}
            last_close.update(closes)
            closes_by_day[day] = {s: float(c) for s, c in closes.items()}
            if bars:
                last_data_date = day
            suspended_symbols = {
                symbol for symbol in ledger.positions
                if universe_svc.is_suspended(symbol, day, as_of=clock.session_close(day))
            }
            ledger.mark_to_market(
                closes, price_date=day, suspended_symbols=suspended_symbols
            )
            snap = ledger.snapshot()
            events.append(
                AccountEvent(
                    ts=clock.session_close(day), trade_date=day,
                    cash=ledger.cash, frozen_cash=ledger.frozen_cash,
                    market_value=ledger.market_value, nav=ledger.nav,
                    realized_pnl=ledger.realized_pnl, unrealized_pnl=ledger.unrealized_pnl,
                    fees=ledger.fees_paid, taxes=ledger.taxes_paid,
                    positions=snap["positions"],
                )
            )
            nav_series.append((day, ledger.nav))

            # --- decision at T close (executes T+1) ---------------------------
            if day == days[-1]:
                continue
            as_of = clock.decision_time(day)
            universe = universe_svc.get_universe(day, as_of=as_of)
            ctx = StrategyContext(
                trade_date=day, as_of=as_of, universe=universe, feed=feed,
                nav=ledger.nav,
                positions={s: p.total for s, p in ledger.positions.items() if p.total > 0},
                params=dict(request.config.get("params", {})),
            )
            decision = strategy.on_close(ctx)
            signal_id = f"sig:{run_id}:{day.isoformat()}"
            target_id = f"tgt:{run_id}:{day.isoformat()}"
            scores_by_day[day] = {s: float(v) for s, v in decision.scores.items()}
            events.append(
                SignalEvent(ts=as_of, trade_date=day, strategy_id=request.strategy_id,
                            signal_id=signal_id, scores=decision.scores,
                            decision_time=as_of,
                            execution_time_assumption=(
                                request.prediction_target.execution_time_assumption
                            ),
                            target_spec=request.prediction_target.target.value,
                            prediction_horizon=request.prediction_target.prediction_horizon)
            )
            if not decision.rebalance:
                continue
            events.append(
                TargetEvent(ts=as_of, trade_date=day, target_id=target_id,
                            strategy_id=request.strategy_id, weights=decision.weights)
            )
            intents = rebalancer.build_intents(
                run_id=run_id, strategy_id=request.strategy_id, target_id=target_id,
                account_id=ledger.account_id, trade_date=day, weights=decision.weights,
                closes=last_close, ledger=ledger, signal_id=signal_id,
            )
            today_value = ZERO
            pending_gross_delta = ZERO
            pending_net_delta = ZERO
            for intent in intents:
                events.append(
                    OrderIntentEvent(
                        ts=as_of, trade_date=day, client_order_id=intent.client_order_id,
                        symbol=intent.symbol, side=intent.side, quantity=intent.quantity,
                        reference_price=intent.reference_price, target_id=target_id,
                    )
                )
                risk_ctx = RiskContext(
                    ledger=ledger, trade_date=day, last_close=last_close,
                    market_data_date=last_data_date,
                    active_order_count=len(oms.active_orders()),
                    today_submitted_value=today_value,
                    seen_client_order_ids=seen_client_ids,
                    pending_gross_delta=pending_gross_delta,
                    pending_net_delta=pending_net_delta,
                )
                rule_violations = risk_engine.evaluate(intent, risk_ctx)
                order = oms.create_order(intent)
                if rule_violations:
                    order.transition(OrderStatus.REJECTED,
                                     reason="; ".join(v.message for v in rule_violations))
                    events.append(
                        RiskRejectedEvent(ts=as_of, trade_date=day,
                                          client_order_id=intent.client_order_id,
                                          violations=[f"{v.rule}: {v.message}" for v in rule_violations])
                    )
                    violations.extend(f"{v.rule}: {v.message}" for v in rule_violations)
                    n_rejects += 1
                    continue
                events.append(RiskApprovedEvent(ts=as_of, trade_date=day,
                                                client_order_id=intent.client_order_id))
                order.transition(OrderStatus.RISK_APPROVED)
                try:
                    if order.side == OrderSide.BUY:
                        security = universe_svc.get_security(intent.symbol, as_of=as_of)
                        if security is None:
                            raise InsufficientCashError("security unavailable for reservation")
                        reservation_rules = self._rulebook.get_rules(
                            security.exchange, security.board, security.security_type,
                            intent.symbol, as_of,
                            is_st=universe_svc.is_st(intent.symbol, day, as_of=as_of),
                            is_suspended=universe_svc.is_suspended(
                                intent.symbol, day, as_of=as_of
                            ),
                            list_date=security.list_date,
                            listed_trading_days=self._listed_trading_days(
                                security.list_date, day
                            ),
                        )
                        ledger.freeze_cash_for_order(
                            order.order_id,
                            reservation_policy.reserve(
                                fee_model, reservation_rules.fee_schedule, order.side,
                                order.quantity, intent.reference_price,
                            ),
                        )
                    # SELL: no evening freeze — T+1 pending settle is only released at
                    # next session open; the ledger enforces availability at fill time.
                except InsufficientCashError as exc:
                    order.transition(OrderStatus.REJECTED, reason=str(exc))
                    events.append(
                        RiskRejectedEvent(ts=as_of, trade_date=day,
                                          client_order_id=intent.client_order_id,
                                          violations=[f"FreezeFailed: {exc}"])
                    )
                    n_rejects += 1
                    continue
                order.transition(OrderStatus.SUBMITTING)
                broker.submit(order)
                order.transition(OrderStatus.SUBMITTED)
                events.append(
                    OrderSubmittedEvent(ts=as_of, trade_date=day, order_id=order.order_id,
                                        client_order_id=order.client_order_id,
                                        symbol=order.symbol, side=order.side,
                                        quantity=order.quantity)
                )
                order.transition(OrderStatus.ACKNOWLEDGED)
                events.append(OrderAcceptedEvent(ts=as_of, trade_date=day,
                                                 order_id=order.order_id,
                                                 client_order_id=order.client_order_id))
                liquidity_volume = feed.historical_average_volume(
                    intent.symbol, end=day, as_of=as_of,
                    window=request.liquidity_window,
                )
                pending.append((order, liquidity_volume))
                seen_client_ids.add(intent.client_order_id)
                ordered_qty += intent.quantity
                order_value = intent.reference_price * intent.quantity
                today_value += order_value
                signed_value = order_value if intent.side == OrderSide.BUY else -order_value
                pending_gross_delta += signed_value
                pending_net_delta += signed_value

        ledger.check_invariants()
        return self._finalize(
            run_id=run_id, request=request, events=events, ledger=ledger, oms=oms,
            nav_series=nav_series, closes_by_day=closes_by_day, scores_by_day=scores_by_day,
            opens_by_day=opens_by_day,
            violations=violations, ordered_qty=ordered_qty, filled_qty=filled_qty,
            traded_value=traded_value, n_rejects=n_rejects, n_partials=n_partials,
        )

    # ------------------------------------------------------------------
    def _execute_order(
        self, *, order: Order, day: date, bars: dict, clock: HistoricalClock,
        exchange: ExchangeSimulator, fee_model: ConfigurableFeeModel,
        universe_svc: UniverseService, ledger: AccountLedger,
        broker: SafetyGatedBrokerGateway, events: EventStore, violations: list[str],
        liquidity_volume: int, corporate_actions: list,
        reference_prices: CorporateActionReferencePriceService,
    ) -> None:
        as_of_open = cn_dt(day, 9, 15)
        bar = bars.get(order.symbol)
        sec = universe_svc.get_security(order.symbol, as_of=as_of_open)
        if sec is None:
            self._cancel(order, day, clock, ledger, events, "UNKNOWN_SECURITY")
            return
        is_st = universe_svc.is_st(order.symbol, day, as_of=as_of_open)
        is_susp = universe_svc.is_suspended(order.symbol, day, as_of=as_of_open) or bar is None
        rules: TradingRules = self._rulebook.get_rules(
            sec.exchange, sec.board, sec.security_type, order.symbol,
            clock.session_open(day), is_st=is_st, is_suspended=is_susp,
            list_date=sec.list_date,
            listed_trading_days=self._listed_trading_days(sec.list_date, day),
        )
        reference_price = reference_prices.calculate(
            bar.prev_close if bar is not None else order.reference_price,
            corporate_actions,
            rule_version=rules.rulebook_version,
            security_status="ST" if is_st else "NORMAL",
        )
        outcome = exchange.execute(
            order, bar, rules, liquidity_volume=liquidity_volume,
            reference_price=reference_price,
        )
        fill_qty, price = outcome.filled_quantity, outcome.price

        if fill_qty > 0 and price is not None and order.side == OrderSide.BUY:
            fill_qty = self._affordable_quantity(
                ledger, order, price, fill_qty, rules.lot_size, fee_model, rules
            )
            if fill_qty <= 0:
                outcome_reason = "INSUFFICIENT_CASH"
                self._cancel(order, day, clock, ledger, events, outcome_reason)
                violations.append(f"{order.symbol}@{day}: {outcome_reason}")
                return

        if fill_qty > 0 and price is not None:
            fees = fee_model.compute(rules.fee_schedule, order.side, fill_qty, price)
            fill = FillRecord(
                fill_id=new_id("fill_"), order_id=order.order_id, symbol=order.symbol,
                side=order.side, quantity=fill_qty, price=price,
                commission=fees.commission, stamp_tax=fees.stamp_tax,
                transfer_fee=fees.transfer_fee, trade_date=day,
                filled_at=clock.session_open(day),
            )
            order.record_fill(fill_qty, price)
            ledger.apply_fill(fill, order_id=order.order_id)
            broker.apply_fill(fill)
            event_cls = FillEvent if order.status == OrderStatus.FILLED else PartialFillEvent
            events.append(
                event_cls(
                    ts=clock.session_open(day), trade_date=day, order_id=order.order_id,
                    fill_id=fill.fill_id, symbol=fill.symbol, side=fill.side,
                    quantity=fill.quantity, price=fill.price, commission=fill.commission,
                    stamp_tax=fill.stamp_tax, transfer_fee=fill.transfer_fee,
                )
            )
        if not order.is_terminal:
            reason = outcome.reason or "DAY_ORDER_EXPIRED"
            if outcome.reason:
                violations.append(f"{order.symbol}@{day}: {outcome.reason}")
            self._cancel(order, day, clock, ledger, events, reason)
        else:
            ledger.unfreeze_cash_for_order(order.order_id)
            ledger.unfreeze_shares_for_order(order.order_id)

    @staticmethod
    def _cancel(order: Order, day: date, clock: HistoricalClock, ledger: AccountLedger,
                events: EventStore, reason: str) -> None:
        order.transition(OrderStatus.CANCELLED, reason=reason)
        ledger.unfreeze_cash_for_order(order.order_id)
        ledger.unfreeze_shares_for_order(order.order_id)
        events.append(
            CancelEvent(ts=clock.session_open(day), trade_date=day, order_id=order.order_id,
                        client_order_id=order.client_order_id, reason=reason)
        )

    @staticmethod
    def _affordable_quantity(
        ledger: AccountLedger, order: Order, price: Decimal, proposed_qty: int,
        lot_size: int, fee_model: ConfigurableFeeModel, rules: TradingRules,
    ) -> int:
        funds = ledger.cash + ledger.frozen_for_order(order.order_id)
        qty = proposed_qty
        while qty > 0:
            fees = fee_model.compute(rules.fee_schedule, order.side, qty, price)
            if price * qty + fees.total <= funds:
                return qty
            qty -= lot_size
        return 0

    def _listed_trading_days(self, list_date: date, current_day: date) -> int:
        calendar = self._provider.trading_calendar(list_date, current_day)
        if calendar and calendar[0] == list_date:
            return len(calendar)
        if (current_day - list_date).days > 31:
            return 10_000
        raise QuantSystemError(
            f"incomplete calendar for IPO rule: {list_date}..{current_day}"
        )

    # ------------------------------------------------------------------
    def _finalize(
        self, *, run_id: str, request: BacktestRequest, events: EventStore,
        ledger: AccountLedger, oms: OrderManagementSystem,
        nav_series: list[tuple[date, Decimal]],
        closes_by_day: dict[date, dict[str, float]],
        opens_by_day: dict[date, dict[str, float]],
        scores_by_day: dict[date, dict[str, float]],
        violations: list[str], ordered_qty: int, filled_qty: int,
        traded_value: Decimal, n_rejects: int, n_partials: int,
    ) -> BacktestResult:
        benchmark_series = (
            self._provider.benchmark_series(request.benchmark, [day for day, _ in nav_series])
            if request.benchmark else None
        )
        performance = compute_performance_metrics(nav_series, request.initial_cash, benchmark_series)
        performance.update(
            compute_prediction_ic(
                scores_by_day, opens_by_day, closes_by_day, request.prediction_target
            )
        )
        execution = compute_execution_metrics(
            ordered_qty, filled_qty, traded_value, ledger.fees_paid, ledger.taxes_paid,
            nav_series, n_rejects, n_partials,
        )
        base_cost = float(ledger.fees_paid + ledger.taxes_paid)
        performance["transaction_cost_sensitivity"] = {
            str(multiplier): {
                "estimated_final_nav": float(ledger.nav)
                - (float(multiplier) - float(request.transaction_cost_multiplier)) * base_cost,
                "estimated_total_return": (
                    float(ledger.nav)
                    - (float(multiplier) - float(request.transaction_cost_multiplier)) * base_cost
                ) / float(request.initial_cash) - 1.0,
            }
            for multiplier in request.cost_stress_multipliers
        }
        risk_metrics = {
            "final_cash": float(ledger.cash),
            "final_market_value": float(ledger.market_value),
            "realized_pnl": float(ledger.realized_pnl),
            "unrealized_pnl": float(ledger.unrealized_pnl),
            "n_violations": len(violations),
            "stale_or_suspended_positions": {
                symbol: {
                    "price_as_of": position["price_as_of"],
                    "stale_price_age": position["stale_price_age"],
                    "is_suspended": position["is_suspended"],
                }
                for symbol, position in ledger.snapshot()["positions"].items()
                if position["stale_price_age"] or position["is_suspended"]
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            equity_lines = ["date,cash,frozen_cash,market_value,nav"]
            account_events = [e for e in events.events if e.event_type == "ACCOUNT"]
            for e in account_events:
                equity_lines.append(
                    f"{e.trade_date.isoformat()},{e.cash},{e.frozen_cash},{e.market_value},{e.nav}"
                )
            (tmpdir / "equity_curve.csv").write_text("\n".join(equity_lines) + "\n")
            events.save_jsonl(str(tmpdir / "events.jsonl"))
            import json

            (tmpdir / "orders.jsonl").write_text(
                "\n".join(json.dumps(o.model_dump(mode="json"), sort_keys=True)
                          for o in oms.all_orders()) + "\n"
            )
            (tmpdir / "trades.jsonl").write_text(
                "\n".join(json.dumps(f.model_dump(mode="json"), sort_keys=True)
                          for f in ledger.fills) + "\n"
            )
            (tmpdir / "positions.json").write_text(json.dumps(ledger.snapshot(), indent=1))
            metrics_doc = {
                "performance": performance,
                "execution": execution,
                "risk": risk_metrics,
                "violations": violations,
            }
            (tmpdir / "metrics.json").write_text(json.dumps(metrics_doc, indent=1, sort_keys=True))
            (tmpdir / "request.json").write_text(
                json.dumps(request.model_dump(mode="json"), indent=1, sort_keys=True)
            )
            uri, content_hash = self._store.put_dir(f"backtests/{run_id}", str(tmpdir))

        parents: tuple[str, ...] = ()
        if request.dataset_snapshot_id:
            parents = (request.dataset_snapshot_id,)
        spec = backtest_spec_artifact(
            producer=self._producer, code_commit=self._code_commit,
            config=request.model_dump(mode="json"), parents=parents,
            data_snapshot_id=request.dataset_snapshot_id or None,
            experiment_id=request.experiment_id,
            content_hash=hash_json(request.model_dump(mode="json")),
            metadata={"run_id": run_id},
        )
        self._registry.register(spec)
        result_env = backtest_result_artifact(
            producer=self._producer, code_commit=self._code_commit, uri=uri,
            content_hash=content_hash,
            parents=parents + (spec.artifact_id,),
            data_snapshot_id=request.dataset_snapshot_id or None,
            experiment_id=request.experiment_id,
            config=request.model_dump(mode="json"),
            metadata={
                "run_id": run_id,
                "event_schema_version": "1.0",
                "event_log_root_hash": events.events[0].event_hash if events.events else "",
                "final_event_hash": events.final_event_hash,
                "performance": performance,
                "execution": execution,
                "risk": risk_metrics,
                "n_violations": len(violations),
            },
        )
        self._registry.register(result_env)

        return BacktestResult(
            run_id=run_id, request=request,
            result_artifact_id=result_env.artifact_id, spec_artifact_id=spec.artifact_id,
            run_dir=uri[len("file://"):],
            performance_metrics=performance, execution_metrics=execution,
            risk_metrics=risk_metrics, violations=violations,
            final_event_hash=events.final_event_hash,
        )

