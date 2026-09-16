from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from tests.helpers import make_engine_with

from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.backtest.request import BacktestRequest
from quant_system.infra.db import create_all, make_engine, make_session_factory
from quant_system.strategies import resolve_strategy


def _fresh_world(tmp_path: Path, tag: str):
    engine = make_engine(f"sqlite:///{tmp_path / f'{tag}.db'}")
    create_all(engine)
    registry = ArtifactRegistry(make_session_factory(engine))
    store = LocalArtifactStore(str(tmp_path / f"store_{tag}"))
    return registry, store


def _run(provider, rulebook, registry, store):
    engine = make_engine_with(
        provider, rulebook, store, registry, resolve_strategy("momentum_topk", {})
    )
    return engine.run(BacktestRequest(
        strategy_id="momentum_topk", dataset_snapshot_id="",
        start_date=date(2024, 1, 1), end_date=date(2024, 4, 30),
        initial_cash=Decimal("1000000"), random_seed=42,
    ))


def test_same_inputs_produce_identical_core_outputs(provider, rulebook, tmp_path):
    """Same data + code + config + seed => identical equity curve and metrics."""
    r1 = _run(provider, rulebook, *_fresh_world(tmp_path, "a"))
    r2 = _run(provider, rulebook, *_fresh_world(tmp_path, "b"))

    eq1 = (Path(r1.run_dir) / "equity_curve.csv").read_bytes()
    eq2 = (Path(r2.run_dir) / "equity_curve.csv").read_bytes()
    assert eq1 == eq2

    m1 = json.loads((Path(r1.run_dir) / "metrics.json").read_text())
    m2 = json.loads((Path(r2.run_dir) / "metrics.json").read_text())
    assert m1 == m2

    assert r1.performance_metrics == r2.performance_metrics
    assert r1.execution_metrics == r2.execution_metrics
