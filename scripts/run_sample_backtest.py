"""End-to-end sample: fixtures -> snapshot -> event-driven backtest ->
replay verification -> promotion validation -> experiment lifecycle.

Uses QUANT_DB_URL if set (PostgreSQL via docker compose); otherwise falls back
to a local SQLite file so the sample runs with zero infrastructure.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_system.artifacts.lineage import ArtifactLineageService  # noqa: E402
from quant_system.artifacts.registry import ArtifactRegistry  # noqa: E402
from quant_system.artifacts.store import LocalArtifactStore  # noqa: E402
from quant_system.backtest.engine import EventDrivenBacktestEngine  # noqa: E402
from quant_system.backtest.replay import ReplayService  # noqa: E402
from quant_system.backtest.request import BacktestRequest  # noqa: E402
from quant_system.data.snapshots.service import DataSnapshotService  # noqa: E402
from quant_system.domain.enums import ExperimentState  # noqa: E402
from quant_system.domain.exceptions import ReplayMismatchError  # noqa: E402
from quant_system.fixtures import generate_fixture_data  # noqa: E402
from quant_system.infra.db import create_all, make_engine, make_session_factory  # noqa: E402
from quant_system.observability.logging import configure_logging  # noqa: E402
from quant_system.research.experiments import ExperimentService  # noqa: E402
from quant_system.research.promotion import (  # noqa: E402
    CostConfigGate,
    CriticalRiskGate,
    ExecutionAssumptionGate,
    LineageGate,
    PromotionPolicy,
    ReplayGate,
    SnapshotIntegrityGate,
)
from quant_system.rules.a_share import ConfigAShareRuleBook  # noqa: E402
from quant_system.strategies import resolve_strategy  # noqa: E402


def main() -> None:
    configure_logging()
    var = ROOT / "var"
    var.mkdir(exist_ok=True)

    db_url = os.environ.get("QUANT_DB_URL_SAMPLE") or os.environ.get(
        "QUANT_DB_URL", f"sqlite:///{var / 'quant.db'}"
    )
    engine = make_engine(db_url)
    create_all(engine)
    session_factory = make_session_factory(engine)

    configured_data_dir = os.environ.get("QUANT_DATA_DIR")
    data_dir = Path(configured_data_dir) if configured_data_dir else var / "fixtures" / "cn_a"
    if not (data_dir / "bars.parquet").exists():
        if configured_data_dir:
            raise FileNotFoundError(f"configured market data not found: {data_dir / 'bars.parquet'}")
        generate_fixture_data(str(data_dir), seed=42)
        print(f"fixtures generated at {data_dir}")
    data_label = "external PIT dataset" if configured_data_dir else "fixture data"
    backtest_start = date.fromisoformat(os.environ.get("QUANT_BACKTEST_START", "2024-01-02"))
    backtest_end = date.fromisoformat(os.environ.get("QUANT_BACKTEST_END", "2024-06-28"))

    store = LocalArtifactStore(str(var / "artifacts"))
    registry = ArtifactRegistry(session_factory)
    lineage = ArtifactLineageService(registry)
    snapshots = DataSnapshotService(store, registry)
    experiments = ExperimentService(session_factory)

    snapshot = snapshots.create_snapshot(str(data_dir), producer="sample-script")
    print(f"data snapshot: {snapshot.artifact_id}")

    exp = experiments.create("momentum_topk sample", "framework exercise, not alpha")
    for state in (ExperimentState.PROPOSED, ExperimentState.SPEC_READY, ExperimentState.RUNNING):
        experiments.transition(exp.experiment_id, state, reason="sample flow")

    from quant_system.data.providers.local_parquet import LocalParquetProvider

    provider = LocalParquetProvider(str(data_dir))
    rulebook = ConfigAShareRuleBook(str(ROOT / "configs" / "markets" / "cn_a.yaml"))
    bt_engine = EventDrivenBacktestEngine(
        provider=provider, rulebook=rulebook, store=store, registry=registry,
        strategy_resolver=resolve_strategy,
    )
    request = BacktestRequest(
        strategy_id="momentum_topk",
        dataset_snapshot_id=snapshot.artifact_id,
        start_date=backtest_start,
        end_date=backtest_end,
        initial_cash=Decimal("1000000"),
        experiment_id=exp.experiment_id,
        config={"params": {"lookback_days": 20, "top_k": 3, "exclude_st": True}},
    )
    result = bt_engine.run(request)
    print(f"backtest run: {result.run_id}")
    print(f"  result artifact: {result.result_artifact_id}")
    print(f"  run dir: {result.run_dir}")

    replay = ReplayService()
    try:
        checked = replay.replay_and_verify(
            str(Path(result.run_dir) / "events.jsonl"), request.initial_cash
        )
        replay_ok = True
        print(f"  replay verified: {checked} account checkpoints match")
    except ReplayMismatchError as exc:
        replay_ok = False
        print(f"  REPLAY FAILED: {exc}")

    policy = PromotionPolicy(
        gates=[
            LineageGate(lineage),
            SnapshotIntegrityGate(registry, snapshots),
            CostConfigGate(),
            ExecutionAssumptionGate(),
            CriticalRiskGate(),
            ReplayGate(),
        ],
        thresholds=PromotionPolicy.load_thresholds(
            str(ROOT / "configs" / "promotion" / "default.yaml")
        ),
    )
    report = policy.validate(
        performance=result.performance_metrics,
        execution=result.execution_metrics,
        context={
            "result_artifact_id": result.result_artifact_id,
            "data_snapshot_id": snapshot.artifact_id,
            "request": request.model_dump(mode="json"),
            "violations": result.violations,
            "replay_ok": replay_ok,
        },
    )
    experiments.record_run(
        exp.experiment_id,
        code_commit="unknown",
        config=request.model_dump(mode="json"),
        data_snapshot_id=snapshot.artifact_id,
        input_artifact_ids=[snapshot.artifact_id],
        output_artifact_ids=[result.result_artifact_id],
        random_seed=request.random_seed,
        status="SUCCEEDED",
        metrics=result.performance_metrics,
    )
    experiments.transition(exp.experiment_id, ExperimentState.COMPLETED, reason="run finished")
    experiments.transition(exp.experiment_id, ExperimentState.VALIDATING, reason="promotion check")
    if report.passed:
        experiments.transition(exp.experiment_id, ExperimentState.CANDIDATE,
                               reason="all gates passed")
    else:
        experiments.transition(exp.experiment_id, ExperimentState.REJECTED,
                               reason="; ".join(report.threshold_failures) or "gate failure")

    perf = result.performance_metrics
    exe = result.execution_metrics
    print(f"\n=== sample backtest summary ({data_label} — NOT a profitability claim) ===")
    print(f"  total_return      : {perf['total_return']:+.4%}")
    print(f"  annualized_return : {perf['annualized_return']:+.4%}")
    print(f"  sharpe            : {perf['sharpe']:.3f}")
    print(f"  max_drawdown      : {perf['max_drawdown']:.4%}")
    print(f"  fill_ratio        : {exe['fill_ratio']:.3f}")
    print(f"  transaction_cost  : {exe['transaction_cost']:.2f}")
    print(f"  rejected_orders   : {exe['rejected_orders']}")
    print(f"  partial_fills     : {exe['partial_fills']}")
    print(f"  n_violations      : {len(result.violations)}")
    print(f"  validation passed : {report.passed}")
    for g in report.gate_results:
        print(f"    gate {g.gate:<32} {'PASS' if g.passed else 'FAIL'} {g.detail}")
    if report.threshold_failures:
        for t in report.threshold_failures:
            print(f"    threshold: {t}")
    print(f"  experiment state  : {experiments.get(exp.experiment_id).state.value}")


if __name__ == "__main__":
    main()
