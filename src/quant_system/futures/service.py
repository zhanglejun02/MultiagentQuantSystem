"""Application service for persistent local futures paper simulations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from quant_system.domain.types import new_id, utc_now
from quant_system.futures.account import FuturesAccountLedger
from quant_system.futures.contracts import FuturesContractCatalog
from quant_system.futures.data import FixtureFuturesData
from quant_system.futures.domain import (
    FuturesOrder,
    FuturesOrderIntent,
    FuturesOrderStatus,
    FuturesTrade,
    SimulationStatus,
)
from quant_system.futures.events import verify_event_chain
from quant_system.futures.oms import FuturesOMS
from quant_system.futures.paper import DailyPaperMatcher
from quant_system.futures.repository import FuturesSimulationRepository
from quant_system.futures.risk import FuturesPreTradeRisk
from quant_system.risk.portfolio import RiskContribution
from quant_system.risk.runtime import TradingRuntime


@dataclass
class SimulationRuntime:
    simulation_id: str
    name: str
    status: SimulationStatus
    current_index: int
    current_trading_day: date | None
    created_at: datetime
    updated_at: datetime
    marks: dict[str, Decimal]
    ledger: FuturesAccountLedger
    oms: FuturesOMS
    trades: list[FuturesTrade]
    settlements: list[dict[str, Any]]
    event_seq: int = 0
    event_hash: str = ""


class FuturesSimulationService:
    def __init__(
        self,
        repository: FuturesSimulationRepository,
        catalog: FuturesContractCatalog,
        data: FixtureFuturesData,
        runtime: TradingRuntime,
        *,
        risk: FuturesPreTradeRisk | None = None,
        matcher: DailyPaperMatcher | None = None,
        id_factory: Callable[[str], str] = new_id,
        now_provider: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.data = data
        self.runtime = runtime
        self.risk = risk or FuturesPreTradeRisk(runtime)
        self.matcher = matcher or DailyPaperMatcher()
        self._id_factory = id_factory
        self._now = now_provider
        self._contracts = {contract.symbol: contract for contract in catalog.all()}

    def create(
        self,
        *,
        name: str,
        initial_balance: Decimal,
        mode: str = "PAPER",
    ) -> dict[str, Any]:
        if mode != "PAPER":
            raise ValueError("only PAPER futures simulations are supported")
        now = self._now()
        runtime = SimulationRuntime(
            simulation_id=self._id_factory("fsim_"),
            name=name.strip() or "期货仿真",
            status=SimulationStatus.RUNNING,
            current_index=-1,
            current_trading_day=None,
            created_at=now,
            updated_at=now,
            marks=self.data.initial_marks(),
            ledger=FuturesAccountLedger(initial_balance),
            oms=FuturesOMS(id_factory=self._id_factory, now_provider=self._now),
            trades=[],
            settlements=[],
        )
        state = self._state(runtime)
        event = self.repository.create(
            simulation_id=runtime.simulation_id,
            name=runtime.name,
            initial_balance=str(initial_balance),
            state=state,
            occurred_at=now,
        )
        runtime.event_seq = event.seq
        runtime.event_hash = event.event_hash
        return self.snapshot(runtime.simulation_id)

    def list_simulations(self) -> list[dict[str, Any]]:
        return self.repository.list_summaries()

    def contracts(self) -> list[dict[str, Any]]:
        return [
            contract.model_dump(mode="json") | {"catalog_version": self.catalog.version}
            for contract in self.catalog.all()
        ]

    def snapshot(self, simulation_id: str) -> dict[str, Any]:
        runtime = self._load(simulation_id)
        return self._public_snapshot(runtime)

    def orders(self, simulation_id: str) -> list[dict[str, Any]]:
        runtime = self._load(simulation_id)
        return [order.model_dump(mode="json") for order in runtime.oms.all()]

    def trades(self, simulation_id: str) -> list[dict[str, Any]]:
        runtime = self._load(simulation_id)
        return [trade.model_dump(mode="json") for trade in runtime.trades]

    def settlements(self, simulation_id: str) -> list[dict[str, Any]]:
        return list(self._load(simulation_id).settlements)

    def events(self, simulation_id: str) -> list[dict[str, Any]]:
        return [event.model_dump(mode="json") for event in self.repository.events(simulation_id)]

    def submit_order(
        self,
        simulation_id: str,
        intent: FuturesOrderIntent,
    ) -> dict[str, Any]:
        runtime = self._load(simulation_id)
        existing = runtime.oms.get_by_client_id(intent.client_order_id)
        if existing is not None:
            return existing.model_dump(mode="json")
        if runtime.status == SimulationStatus.COMPLETED:
            raise ValueError("simulation is completed")
        contract = self.catalog.get(intent.symbol)
        mark = runtime.marks[intent.symbol]
        decision = self.risk.evaluate(
            intent=intent,
            contract=contract,
            ledger=runtime.ledger,
            mark=mark,
            marks=runtime.marks,
            active_orders=len(runtime.oms.active()),
            margin_call=runtime.status == SimulationStatus.MARGIN_CALL,
            portfolio_contribution=self._risk_contribution(runtime),
        )
        order = runtime.oms.create(intent)
        if decision.approved:
            order = runtime.oms.accept(order.order_id)
            event_type = "ORDER_ACCEPTED"
        else:
            order = runtime.oms.reject(order.order_id, decision.reason)
            event_type = "ORDER_REJECTED"
        self._commit(runtime, event_type, {"order_id": order.order_id})
        return order.model_dump(mode="json")

    def cancel_order(self, simulation_id: str, order_id: str) -> dict[str, Any]:
        runtime = self._load(simulation_id)
        order = runtime.oms.cancel(order_id)
        self._commit(runtime, "ORDER_CANCELLED", {"order_id": order_id})
        return order.model_dump(mode="json")

    def advance(self, simulation_id: str) -> dict[str, Any]:
        runtime = self._load(simulation_id)
        next_index = runtime.current_index + 1
        if next_index >= len(self.data.trading_days):
            raise ValueError("no more fixture trading days")
        trading_day = self.data.trading_days[next_index]
        bars = self.data.bars_for_day(trading_day)
        new_trades: list[str] = []
        for order in list(runtime.oms.active()):
            if order.status not in {
                FuturesOrderStatus.ACCEPTED,
                FuturesOrderStatus.PARTIALLY_FILLED,
            }:
                continue
            bar = bars.get(order.symbol)
            if bar is None:
                continue
            contract = self.catalog.get(order.symbol)
            intent = FuturesOrderIntent(
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                direction=order.direction,
                offset=order.offset,
                quantity=order.remaining_quantity,
                order_type=order.order_type,
                limit_price=order.limit_price,
                created_at=order.created_at,
            )
            execution_marks = dict(runtime.marks)
            execution_marks[order.symbol] = bar.open
            decision = self.risk.evaluate(
                intent=intent,
                contract=contract,
                ledger=runtime.ledger,
                mark=bar.open,
                marks=execution_marks,
                active_orders=0,
                margin_call=runtime.status == SimulationStatus.MARGIN_CALL,
                portfolio_contribution=self._risk_contribution(runtime),
            )
            if not decision.approved:
                runtime.oms.reject(order.order_id, f"execution risk: {decision.reason}")
                continue
            match = self.matcher.match(order, bar, contract)
            if match is None:
                continue
            fee = contract.fee(order.offset, match.quantity)
            runtime.ledger.apply_trade(
                contract=contract,
                direction=order.direction,
                offset=order.offset,
                quantity=match.quantity,
                price=match.price,
                fee=fee,
                trading_day=trading_day,
            )
            runtime.oms.record_fill(order.order_id, match.quantity, match.price)
            trade = FuturesTrade(
                trade_id=self._id_factory("ftrd_"),
                order_id=order.order_id,
                symbol=order.symbol,
                direction=order.direction,
                offset=order.offset,
                quantity=match.quantity,
                price=match.price,
                fee=fee,
                trading_day=trading_day,
                traded_at=self._now(),
            )
            runtime.trades.append(trade)
            new_trades.append(trade.trade_id)

        settlements = {symbol: bar.settlement for symbol, bar in bars.items()}
        runtime.ledger.settle(
            trading_day=trading_day,
            settlements=settlements,
            contracts=self._contracts,
        )
        runtime.marks = settlements
        account = runtime.ledger.snapshot(runtime.marks)
        if account.available < 0 or account.risk_ratio >= Decimal("0.85"):
            runtime.status = SimulationStatus.MARGIN_CALL
        elif next_index == len(self.data.trading_days) - 1:
            runtime.status = SimulationStatus.COMPLETED
        else:
            runtime.status = SimulationStatus.RUNNING
        runtime.current_index = next_index
        runtime.current_trading_day = trading_day
        runtime.settlements.append(
            {
                "trading_day": trading_day.isoformat(),
                "balance": str(account.balance),
                "equity": str(account.equity),
                "available": str(account.available),
                "margin": str(account.margin),
                "risk_ratio": str(account.risk_ratio),
                "settlements": {symbol: str(price) for symbol, price in settlements.items()},
            }
        )
        self._commit(
            runtime,
            "TRADING_DAY_SETTLED",
            {"trading_day": trading_day.isoformat(), "trade_ids": new_trades},
        )
        return self._public_snapshot(runtime)

    def _risk_contribution(self, runtime: SimulationRuntime) -> RiskContribution:
        gross = Decimal("0")
        net = Decimal("0")
        for position in runtime.ledger.positions():
            contract = self._contracts[position.symbol]
            notional_per_lot = runtime.marks[position.symbol] * contract.multiplier
            gross += notional_per_lot * (
                position.long_quantity + position.short_quantity
            )
            net += notional_per_lot * (
                position.long_quantity - position.short_quantity
            )
        snapshot = runtime.ledger.snapshot(runtime.marks)
        return RiskContribution(
            account_id=f"futures:{runtime.simulation_id}",
            strategy_id=f"futures:{runtime.simulation_id}",
            asset_class="FUTURES",
            nav=snapshot.equity,
            gross_exposure=gross,
            net_exposure=net,
            margin=snapshot.margin,
        )

    def _load(self, simulation_id: str) -> SimulationRuntime:
        state, seq, final_hash = self.repository.get_state(simulation_id)
        events = self.repository.events(simulation_id)
        if verify_event_chain(events) != final_hash:
            raise ValueError("futures event head does not match simulation snapshot")
        if not events or events[-1].payload.get("state") != state:
            raise ValueError("futures event state does not match simulation snapshot")
        contracts = self._contracts
        ledger = FuturesAccountLedger.from_state(state["ledger"], contracts)
        oms = FuturesOMS(id_factory=self._id_factory, now_provider=self._now)
        oms.restore([FuturesOrder.model_validate(raw) for raw in state["orders"]])
        return SimulationRuntime(
            simulation_id=simulation_id,
            name=str(state["name"]),
            status=SimulationStatus(state["status"]),
            current_index=int(state["current_index"]),
            current_trading_day=(
                date.fromisoformat(state["current_trading_day"])
                if state.get("current_trading_day")
                else None
            ),
            created_at=datetime.fromisoformat(state["created_at"]),
            updated_at=datetime.fromisoformat(state["updated_at"]),
            marks={symbol: Decimal(str(mark)) for symbol, mark in state["marks"].items()},
            ledger=ledger,
            oms=oms,
            trades=[FuturesTrade.model_validate(raw) for raw in state["trades"]],
            settlements=list(state["settlements"]),
            event_seq=seq,
            event_hash=final_hash,
        )

    def _commit(
        self,
        runtime: SimulationRuntime,
        event_type: str,
        details: dict[str, Any],
    ) -> None:
        runtime.updated_at = self._now()
        state = self._state(runtime)
        event = self.repository.append(
            simulation_id=runtime.simulation_id,
            expected_seq=runtime.event_seq,
            event_type=event_type,
            state=state,
            occurred_at=runtime.updated_at,
            details=details,
        )
        runtime.event_seq = event.seq
        runtime.event_hash = event.event_hash

    def _state(self, runtime: SimulationRuntime) -> dict[str, Any]:
        return {
            "simulation_id": runtime.simulation_id,
            "name": runtime.name,
            "mode": "PAPER",
            "status": runtime.status.value,
            "current_index": runtime.current_index,
            "current_trading_day": (
                runtime.current_trading_day.isoformat() if runtime.current_trading_day else None
            ),
            "created_at": runtime.created_at.isoformat(),
            "updated_at": runtime.updated_at.isoformat(),
            "marks": {symbol: str(mark) for symbol, mark in runtime.marks.items()},
            "ledger": runtime.ledger.export_state(),
            "orders": [order.model_dump(mode="json") for order in runtime.oms.all()],
            "trades": [trade.model_dump(mode="json") for trade in runtime.trades],
            "settlements": runtime.settlements,
        }

    def _public_snapshot(self, runtime: SimulationRuntime) -> dict[str, Any]:
        account = runtime.ledger.snapshot(runtime.marks)
        return {
            "simulation_id": runtime.simulation_id,
            "name": runtime.name,
            "mode": "PAPER",
            "status": runtime.status.value,
            "current_trading_day": (
                runtime.current_trading_day.isoformat() if runtime.current_trading_day else None
            ),
            "next_trading_day": (
                self.data.trading_days[runtime.current_index + 1].isoformat()
                if runtime.current_index + 1 < len(self.data.trading_days)
                else None
            ),
            "account": {
                key: str(value)
                for key, value in account.__dict__.items()
            },
            "marks": {symbol: str(mark) for symbol, mark in runtime.marks.items()},
            "positions": [
                {
                    "symbol": position.symbol,
                    "long_today": position.long_today,
                    "long_yesterday": position.long_yesterday,
                    "short_today": position.short_today,
                    "short_yesterday": position.short_yesterday,
                    "long_settlement_price": str(position.long_settlement_price),
                    "short_settlement_price": str(position.short_settlement_price),
                }
                for position in runtime.ledger.positions()
                if position.long_quantity or position.short_quantity
            ],
            "order_count": len(runtime.oms.all()),
            "trade_count": len(runtime.trades),
            "settlement_count": len(runtime.settlements),
            "last_event_seq": runtime.event_seq,
            "last_event_hash": runtime.event_hash,
            "updated_at": runtime.updated_at.isoformat(),
        }