"""Run the deterministic commodity-futures fixture backtest."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_system.artifacts.registry import ArtifactRegistry  # noqa: E402
from quant_system.artifacts.store import LocalArtifactStore  # noqa: E402
from quant_system.futures.backtest import (  # noqa: E402
    FuturesBacktestEngine,
    FuturesBacktestRequest,
)
from quant_system.futures.contracts import FuturesContractCatalog  # noqa: E402
from quant_system.futures.data import FixtureFuturesData  # noqa: E402
from quant_system.infra.db import create_all, make_engine, make_session_factory  # noqa: E402


def main() -> None:
    var = ROOT / "var"
    var.mkdir(exist_ok=True)
    database_url = os.environ.get("QUANT_DB_URL", f"sqlite:///{var / 'quant.db'}")
    database = make_engine(database_url)
    create_all(database)
    registry = ArtifactRegistry(make_session_factory(database))
    engine = FuturesBacktestEngine(
        catalog=FuturesContractCatalog.from_yaml(ROOT / "configs/futures/contracts.yaml"),
        data=FixtureFuturesData(),
        store=LocalArtifactStore(str(var / "artifacts")),
        registry=registry,
    )
    result = engine.run(FuturesBacktestRequest())
    print(f"futures backtest: {result.run_id}")
    print(f"  result artifact: {result.result_artifact_id}")
    print(f"  final event hash: {result.final_event_hash}")
    print(f"  final equity: {result.metrics['final_equity']}")
    print(f"  total return: {result.metrics['total_return']:+.4%}")
    print("  fixture only; not a profitability claim")


if __name__ == "__main__":
    main()