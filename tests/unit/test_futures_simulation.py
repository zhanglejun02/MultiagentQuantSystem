from decimal import Decimal
from pathlib import Path

from quant_system.domain.types import utc_now
from quant_system.futures.contracts import FuturesContractCatalog
from quant_system.futures.data import FixtureFuturesData
from quant_system.futures.domain import Direction, FuturesOrderIntent, Offset
from quant_system.futures.repository import FuturesSimulationRepository
from quant_system.futures.service import FuturesSimulationService
from quant_system.infra.db import create_all, make_engine, make_session_factory
from quant_system.risk.runtime import KillSwitch


def _service(tmp_path: Path) -> FuturesSimulationService:
    engine = make_engine(f"sqlite:///{tmp_path / 'futures.db'}")
    create_all(engine)
    repository = FuturesSimulationRepository(make_session_factory(engine))
    catalog = FuturesContractCatalog.from_yaml("configs/futures/contracts.yaml")
    return FuturesSimulationService(
        repository,
        catalog,
        FixtureFuturesData(),
        KillSwitch(),
    )


def test_paper_simulation_fills_settles_and_restores(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create(name="test", initial_balance=Decimal("100000"))
    simulation_id = created["simulation_id"]
    intent = FuturesOrderIntent(
        client_order_id="open-rb-long",
        symbol="rb2701.SHFE",
        direction=Direction.LONG,
        offset=Offset.OPEN,
        quantity=2,
        created_at=utc_now(),
    )

    accepted = service.submit_order(simulation_id, intent)
    duplicate = service.submit_order(simulation_id, intent)
    assert accepted["status"] == "ACCEPTED"
    assert duplicate["order_id"] == accepted["order_id"]

    advanced = service.advance(simulation_id)
    assert advanced["current_trading_day"] == "2026-09-07"
    assert advanced["positions"][0]["long_yesterday"] == 2
    assert advanced["trade_count"] == 1
    assert advanced["settlement_count"] == 1

    restored = _service(tmp_path).snapshot(simulation_id)
    assert restored == advanced
    assert [event["event_type"] for event in service.events(simulation_id)] == [
        "SIMULATION_CREATED",
        "ORDER_ACCEPTED",
        "TRADING_DAY_SETTLED",
    ]