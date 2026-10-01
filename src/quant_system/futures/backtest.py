"""Deterministic futures backtest using the authoritative paper-trading core."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from quant_system.artifacts.envelope import hash_json
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import ArtifactStore
from quant_system.artifacts.types import (
    futures_backtest_result_artifact,
    futures_backtest_spec_artifact,
)
from quant_system.domain.types import new_id
from quant_system.futures.contracts import FuturesContractCatalog
from quant_system.futures.data import FixtureFuturesData
from quant_system.futures.domain import Direction, FuturesOrderIntent, Offset
from quant_system.futures.repository import FuturesSimulationRepository
from quant_system.futures.service import FuturesSimulationService
from quant_system.infra.db import create_all, make_engine, make_session_factory
from quant_system.risk.runtime import KillSwitch, TradingRuntime


class FuturesBacktestRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy_id: str = "moving_average_trend"
    symbol: str = "rb2701.SHFE"
    initial_balance: Decimal = Decimal("100000")
    lots: int = 2
    fast_window: int = 2
    slow_window: int = 3
    random_seed: int = 42

    @field_validator("lots", "fast_window", "slow_window")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("lots and windows must be positive")
        return value


class FuturesBacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    spec_artifact_id: str
    result_artifact_id: str
    run_dir: str
    final_event_hash: str
    metrics: dict[str, float | int | str]


class FuturesStrategy(Protocol):
    def target_lots(self, closes: list[Decimal]) -> int: ...


class MovingAverageTrendStrategy:
    def __init__(self, *, fast_window: int, slow_window: int, lots: int) -> None:
        if fast_window >= slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.lots = lots

    def target_lots(self, closes: list[Decimal]) -> int:
        if len(closes) < self.slow_window:
            return 0
        fast = sum(closes[-self.fast_window :], Decimal("0")) / self.fast_window
        slow = sum(closes[-self.slow_window :], Decimal("0")) / self.slow_window
        if fast > slow:
            return self.lots
        if fast < slow:
            return -self.lots
        return 0


class FuturesBacktestEngine:
    def __init__(
        self,
        *,
        catalog: FuturesContractCatalog,
        data: FixtureFuturesData,
        store: ArtifactStore,
        registry: ArtifactRegistry,
        producer: str = "futures-backtest-engine",
        code_commit: str = "unknown",
    ) -> None:
        self.catalog = catalog
        self.data = data
        self.store = store
        self.registry = registry
        self.producer = producer
        self.code_commit = code_commit

    def run(self, request: FuturesBacktestRequest) -> FuturesBacktestResult:
        self.catalog.get(request.symbol)
        strategy = MovingAverageTrendStrategy(
            fast_window=request.fast_window,
            slow_window=request.slow_window,
            lots=request.lots,
        )
        id_factory = self._deterministic_ids()
        fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as database_dir:
            engine = make_engine(f"sqlite:///{Path(database_dir) / 'backtest.db'}")
            create_all(engine)
            service = FuturesSimulationService(
                FuturesSimulationRepository(make_session_factory(engine)),
                self.catalog,
                self.data,
                TradingRuntime(KillSwitch()),
                id_factory=id_factory,
                now_provider=lambda: fixed_now,
            )
            created = service.create(
                name=f"{request.strategy_id}:{request.symbol}",
                initial_balance=request.initial_balance,
            )
            simulation_id = str(created["simulation_id"])
            closes: list[Decimal] = []
            equity: list[tuple[str, Decimal]] = []
            command_index = 0
            for trading_day in self.data.trading_days:
                target = strategy.target_lots(closes)
                command_index = self._rebalance(
                    service,
                    simulation_id,
                    request.symbol,
                    target,
                    command_index,
                    fixed_now,
                )
                snapshot = service.advance(simulation_id)
                equity.append((trading_day.isoformat(), Decimal(snapshot["account"]["equity"])))
                closes.append(self.data.bars_for_day(trading_day)[request.symbol].close)

            snapshot = service.snapshot(simulation_id)
            orders = service.orders(simulation_id)
            trades = service.trades(simulation_id)
            settlements = service.settlements(simulation_id)
            events = service.events(simulation_id)

        metrics = self._metrics(equity, request.initial_balance, snapshot, trades)
        run_id = new_id("fbt_")
        with tempfile.TemporaryDirectory() as output_dir:
            path = Path(output_dir)
            (path / "request.json").write_text(
                json.dumps(request.model_dump(mode="json"), indent=1, sort_keys=True)
            )
            (path / "metrics.json").write_text(json.dumps(metrics, indent=1, sort_keys=True))
            (path / "events.jsonl").write_text(self._jsonl(events))
            (path / "orders.jsonl").write_text(self._jsonl(orders))
            (path / "trades.jsonl").write_text(self._jsonl(trades))
            (path / "settlements.jsonl").write_text(self._jsonl(settlements))
            (path / "positions.json").write_text(
                json.dumps(snapshot, indent=1, sort_keys=True, ensure_ascii=False)
            )
            equity_lines = ["date,equity"] + [f"{day},{value}" for day, value in equity]
            (path / "equity_curve.csv").write_text("\n".join(equity_lines) + "\n")
            uri, content_hash = self.store.put_dir(f"futures_backtests/{run_id}", output_dir)

        request_dict = request.model_dump(mode="json")
        spec = futures_backtest_spec_artifact(
            producer=self.producer,
            code_commit=self.code_commit,
            content_hash=hash_json(request_dict),
            config=request_dict,
            metadata={"run_id": run_id, "catalog_version": self.catalog.version},
        )
        self.registry.register(spec)
        result = futures_backtest_result_artifact(
            producer=self.producer,
            code_commit=self.code_commit,
            uri=uri,
            content_hash=content_hash,
            parents=(spec.artifact_id,),
            config=request_dict,
            metadata={
                "run_id": run_id,
                "final_event_hash": snapshot["last_event_hash"],
                "metrics": metrics,
            },
        )
        self.registry.register(result)
        return FuturesBacktestResult(
            run_id=run_id,
            spec_artifact_id=spec.artifact_id,
            result_artifact_id=result.artifact_id,
            run_dir=uri.removeprefix("file://"),
            final_event_hash=str(snapshot["last_event_hash"]),
            metrics=metrics,
        )

    @staticmethod
    def _rebalance(
        service: FuturesSimulationService,
        simulation_id: str,
        symbol: str,
        target: int,
        command_index: int,
        now: datetime,
    ) -> int:
        snapshot = service.snapshot(simulation_id)
        position = next(
            (item for item in snapshot["positions"] if item["symbol"] == symbol),
            None,
        )
        long_quantity = int(position["long_yesterday"]) if position else 0
        short_quantity = int(position["short_yesterday"]) if position else 0
        target_long = max(target, 0)
        target_short = max(-target, 0)
        commands: list[tuple[Direction, Offset, int]] = []
        if long_quantity > target_long:
            commands.append((Direction.LONG, Offset.CLOSE_YESTERDAY, long_quantity - target_long))
        if short_quantity > target_short:
            commands.append((Direction.SHORT, Offset.CLOSE_YESTERDAY, short_quantity - target_short))
        if long_quantity < target_long:
            commands.append((Direction.LONG, Offset.OPEN, target_long - long_quantity))
        if short_quantity < target_short:
            commands.append((Direction.SHORT, Offset.OPEN, target_short - short_quantity))
        for direction, offset, quantity in commands:
            command_index += 1
            service.submit_order(
                simulation_id,
                FuturesOrderIntent(
                    client_order_id=f"backtest-{command_index:06d}",
                    symbol=symbol,
                    direction=direction,
                    offset=offset,
                    quantity=quantity,
                    created_at=now,
                ),
            )
        return command_index

    @staticmethod
    def _metrics(
        equity: list[tuple[str, Decimal]],
        initial_balance: Decimal,
        snapshot: dict[str, Any],
        trades: list[dict[str, Any]],
    ) -> dict[str, float | int | str]:
        values = [value for _, value in equity]
        peak = initial_balance
        max_drawdown = Decimal("0")
        for value in values:
            peak = max(peak, value)
            if peak:
                max_drawdown = max(max_drawdown, Decimal("1") - value / peak)
        final_equity = values[-1] if values else initial_balance
        return {
            "final_equity": str(final_equity),
            "total_return": float(final_equity / initial_balance - Decimal("1")),
            "max_drawdown": float(max_drawdown),
            "trade_count": len(trades),
            "fees": str(snapshot["account"]["fees"]),
            "final_risk_ratio": float(snapshot["account"]["risk_ratio"]),
        }

    @staticmethod
    def _deterministic_ids() -> Callable[[str], str]:
        sequence = 0

        def generate(prefix: str) -> str:
            nonlocal sequence
            sequence += 1
            return f"{prefix}{sequence:010d}"

        return generate

    @staticmethod
    def _jsonl(rows: list[dict[str, Any]]) -> str:
        if not rows:
            return ""
        return "\n".join(
            json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows
        ) + "\n"