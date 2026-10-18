"""Offline event-driven evaluation of a pinned protocol and frozen dataset."""

import csv
import json
import math
import shutil
import statistics
import tempfile
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from quant_system.artifacts.envelope import ArtifactEnvelope, hash_json
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.artifacts.types import backtest_spec_artifact, research_report_artifact
from quant_system.backtest.engine import EventDrivenBacktestEngine
from quant_system.backtest.replay import LedgerReplayService
from quant_system.backtest.request import BacktestRequest, BacktestResult
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.data.snapshots.service import DataSnapshotService
from quant_system.domain.enums import ArtifactStatus, ExperimentState
from quant_system.domain.exceptions import QuantSystemError
from quant_system.domain.types import bar_available_at
from quant_system.research.baseline.report import render_report
from quant_system.research.baseline.spec import BaselineSpec
from quant_system.research.experiments import ExperimentService
from quant_system.rules.a_share import ConfigAShareRuleBook
from quant_system.strategies.weekly_factor import resolve_research_strategy
from quant_system.strategies.weekly_momentum import resolve_baseline_strategy


def economic_fingerprint(result: BacktestResult) -> str:
    directory = Path(result.run_dir)
    trades = [
        json.loads(line) for line in (directory / "trades.jsonl").read_text().splitlines() if line.strip()
    ]
    semantic_trades = [
        {key: value for key, value in trade.items() if key not in {"fill_id", "order_id"}} for trade in trades
    ]
    return hash_json(
        {
            "equity": (directory / "equity_curve.csv").read_text(),
            "metrics": json.loads((directory / "metrics.json").read_text()),
            "trades": semantic_trades,
        }
    )


class BaselineRunner:
    def __init__(
        self,
        *,
        store: LocalArtifactStore,
        registry: ArtifactRegistry,
        experiments: ExperimentService,
        rulebook_path: Path,
        code_commit: str,
        code_hash: str,
        environment: dict[str, str],
        code_bundle_path: Path | None = None,
    ):
        self.store = store
        self.registry = registry
        self.experiments = experiments
        self.rulebook_path = rulebook_path
        self.code_commit = code_commit
        self.code_hash = code_hash
        self.environment = environment
        self.code_bundle_path = code_bundle_path

    def run(
        self,
        spec: BaselineSpec,
        snapshot_id: str,
        *,
        strategy_expression: str | None = None,
        research_evidence_ids: tuple[str, ...] = (),
    ) -> ArtifactEnvelope:
        snapshots = DataSnapshotService(self.store, self.registry)
        data_path = snapshots.resolve_path(snapshot_id)
        snapshot = self.registry.get(snapshot_id)
        metadata = json.loads((data_path / "dataset_metadata.json").read_text())
        protocol = spec.model_dump(mode="json")
        dataset_protocol_hash = hash_json(protocol)
        if strategy_expression is not None:
            protocol = {
                **protocol,
                "strategy_expression": strategy_expression,
                "research_evidence_ids": list(research_evidence_ids),
            }
        protocol_hash = hash_json(protocol)
        if metadata.get("kind") != "real_fixed_basket_baseline_v1" or metadata.get("source") != "Tushare Pro":
            raise QuantSystemError("baseline requires an explicitly labeled real research dataset")
        if metadata.get("protocol_hash") != dataset_protocol_hash:
            raise QuantSystemError("frozen dataset is bound to a different research protocol")
        provider = LocalParquetProvider(str(data_path))
        self._check_coverage(spec, provider)
        experiment = self.experiments.create(
            spec.name,
            "Fixed-parameter research-only chronological baseline",
            metadata={
                "protocol_hash": protocol_hash,
                "data_snapshot_id": snapshot_id,
                "code_hash": self.code_hash,
            },
        )
        plan_uri, plan_hash = self.store.put_content_addressed_json("research/baseline/plans", protocol)
        plan = self.registry.register(
            backtest_spec_artifact(
                producer="baseline-protocol",
                uri=plan_uri,
                content_hash=plan_hash,
                config=protocol,
                code_commit=self.code_commit,
                data_snapshot_id=snapshot_id,
                experiment_id=experiment.experiment_id,
                parents=(snapshot_id, *research_evidence_ids),
                status=ArtifactStatus.VERIFIED,
                metadata={"kind": "pre_evaluation_baseline_protocol", "code_hash": self.code_hash},
            )
        )
        for state in (ExperimentState.PROPOSED, ExperimentState.SPEC_READY, ExperimentState.RUNNING):
            self.experiments.transition(experiment.experiment_id, state, reason="fixed baseline protocol")
        engine = EventDrivenBacktestEngine(
            provider=provider,
            rulebook=ConfigAShareRuleBook(str(self.rulebook_path)),
            store=self.store,
            registry=self.registry,
            strategy_resolver=resolve_research_strategy
            if strategy_expression is not None
            else resolve_baseline_strategy,
            code_commit=self.code_commit,
            producer="baseline-event-driven-backtest",
        )
        segments: list[dict[str, Any]] = []
        receipts = [snapshot_id, plan.artifact_id, *research_evidence_ids]
        candidate_strategy = "weekly_factor" if strategy_expression is not None else "weekly_momentum"
        try:
            for segment in spec.segments:
                records = {}
                for label, strategy, multiplier in (
                    ("momentum", candidate_strategy, "1"),
                    ("equal_weight", "weekly_equal_weight", "1"),
                    ("cost_stress", candidate_strategy, "2"),
                ):
                    request = BacktestRequest(
                        strategy_id=strategy,
                        dataset_snapshot_id=snapshot_id,
                        start_date=segment.start,
                        end_date=segment.end,
                        initial_cash=spec.initial_cash,
                        benchmark=spec.benchmark,
                        transaction_cost_multiplier=Decimal(multiplier),
                        experiment_id=experiment.experiment_id,
                        config={
                            "params": {
                                "lookback_days": spec.lookback_days,
                                "top_k": spec.top_k,
                                **(
                                    {"expression": strategy_expression}
                                    if strategy_expression is not None
                                    else {}
                                ),
                            },
                            "protocol_artifact_id": plan.artifact_id,
                            "code_hash": self.code_hash,
                            "segment": segment.name,
                            "research_only": True,
                        },
                    )
                    record, artifact_ids = self._evaluate(
                        engine,
                        provider,
                        request,
                        repeat=label != "cost_stress",
                        context=f"{segment.name}/{label}",
                    )
                    records[label] = record
                    receipts.extend(artifact_ids)
                segments.append(
                    {
                        "name": segment.name,
                        "start": segment.start.isoformat(),
                        "end": segment.end.isoformat(),
                        **records,
                    }
                )
            self.experiments.transition(
                experiment.experiment_id,
                ExperimentState.COMPLETED,
                reason="all segments replayed and economic outputs reproduced",
            )
            holdout = segments[-1]
            momentum_return = holdout["momentum"]["performance"]["total_return"]
            equal_return = holdout["equal_weight"]["performance"]["total_return"]
            index_return = holdout["momentum"]["performance"]["benchmark_return"]
            beats_both = momentum_return > max(equal_return, index_return)
            assessment = (
                "Historical holdout outperformed both comparators; limitations prevent deployment or a reliable alpha claim."
                if beats_both
                else "Historical holdout did not outperform both comparators. The experiment is complete; evidence does not justify promoting this strategy."
            )
            payload = {
                "schema_version": "1.0",
                "name": spec.name,
                "research_only": True,
                "deployment_eligible": False,
                "experiment_id": experiment.experiment_id,
                "protocol_artifact_id": plan.artifact_id,
                "protocol_hash": protocol_hash,
                "protocol": protocol,
                "data_snapshot_id": snapshot_id,
                "data_hash": snapshot.content_hash,
                "dataset": metadata,
                "code_commit": self.code_commit,
                "code_hash": self.code_hash,
                "environment": self.environment,
                "segments": segments,
                "assessment": assessment,
                "limitations": list(spec.limitations),
                "technical_acceptance": True,
            }
            if strategy_expression is not None:
                payload["strategy_expression"] = strategy_expression
                payload["research_evidence_ids"] = list(research_evidence_ids)
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                (directory / "report.json").write_text(json.dumps(payload, sort_keys=True, indent=2))
                (directory / "report.md").write_text(render_report(payload))
                if self.code_bundle_path is not None:
                    shutil.copytree(self.code_bundle_path, directory / "code")
                uri, digest = self.store.put_content_addressed_dir("research/baseline/reports", temporary)
            report = self.registry.register(
                research_report_artifact(
                    producer="baseline-research",
                    uri=uri,
                    content_hash=digest,
                    config=protocol,
                    code_commit=self.code_commit,
                    experiment_id=experiment.experiment_id,
                    data_snapshot_id=snapshot_id,
                    parents=tuple(receipts),
                    status=ArtifactStatus.VERIFIED,
                    metadata={
                        "kind": "baseline_research_report",
                        "research_only": True,
                        "deployment_eligible": False,
                        "technical_acceptance": True,
                        "protocol_hash": protocol_hash,
                        "assessment": assessment,
                    },
                )
            )
            self.experiments.record_run(
                experiment.experiment_id,
                code_commit=self.code_commit,
                config=protocol,
                data_snapshot_id=snapshot_id,
                input_artifact_ids=[snapshot_id, plan.artifact_id],
                output_artifact_ids=[report.artifact_id],
                random_seed=42,
                status="SUCCEEDED",
                metrics={"holdout_return": momentum_return, "holdout_beats_both": beats_both},
                logs_uri=uri,
            )
            return report
        except Exception as exc:
            self.experiments.transition(
                experiment.experiment_id, ExperimentState.REJECTED, reason=str(exc)[:500]
            )
            raise

    def _evaluate(
        self,
        engine: EventDrivenBacktestEngine,
        provider: LocalParquetProvider,
        request: BacktestRequest,
        *,
        repeat: bool,
        context: str,
    ) -> tuple[dict[str, Any], list[str]]:
        result = engine.run(request)
        checked = self._verify_replay(result)
        fingerprint = economic_fingerprint(result)
        artifact_ids = []
        duplicate_id = None
        if repeat:
            repeated = engine.run(request)
            self._verify_replay(repeated)
            if fingerprint != economic_fingerprint(repeated):
                raise QuantSystemError(f"economic reproducibility failed: {context}")
            duplicate_id = repeated.result_artifact_id
            artifact_ids.append(duplicate_id)
        artifact_ids.append(result.result_artifact_id)
        record = {
            "artifact_id": result.result_artifact_id,
            "repeat_artifact_id": duplicate_id,
            "run_dir": result.run_dir,
            "performance": result.performance_metrics,
            "execution": result.execution_metrics,
            "ledger_checkpoints": checked,
            "final_event_hash": result.final_event_hash,
            "economic_fingerprint": fingerprint,
            "comparison": self._comparison(result, provider),
            "n_violations": len(result.violations),
            "violation_counts": dict(Counter(violation.split(":", 1)[0] for violation in result.violations)),
        }
        return record, artifact_ids

    @staticmethod
    def _check_coverage(spec: BaselineSpec, provider: LocalParquetProvider) -> None:
        days = provider.trading_calendar(spec.data_start, spec.data_end)
        benchmark_path = provider.data_dir / "benchmarks.parquet"
        benchmark_days = sorted(
            row["trade_date"]
            for row in pq.read_table(benchmark_path).to_pylist()
            if row["symbol"] == spec.benchmark and spec.data_start <= row["trade_date"] <= spec.data_end
        )
        if days != benchmark_days:
            raise QuantSystemError("stock dataset calendar does not match the benchmark calendar")
        provider.benchmark_series(spec.benchmark, days)
        for segment in spec.segments:
            segment_days = [day for day in days if segment.start <= day <= segment.end]
            if len(segment_days) < 10 or (segment.end - segment_days[-1]).days > 7:
                raise QuantSystemError(f"insufficient segment coverage: {segment.name}")
            as_of = bar_available_at(segment_days[0])
            warmup = provider.get_bars(None, spec.data_start, segment_days[0], as_of=as_of)
            for symbol in spec.symbols:
                local_symbol = symbol.replace(".SH", ".SSE")
                if sum(bar.symbol == local_symbol for bar in warmup) <= spec.lookback_days:
                    raise QuantSystemError(f"insufficient warmup for {symbol}/{segment.name}")

    @staticmethod
    def _verify_replay(result: BacktestResult) -> int:
        return LedgerReplayService().replay_and_verify(
            str(Path(result.run_dir) / "events.jsonl"),
            result.request.initial_cash,
            expected_final_hash=result.final_event_hash,
        )

    @staticmethod
    def _comparison(result: BacktestResult, provider: LocalParquetProvider) -> dict[str, float]:
        with (Path(result.run_dir) / "equity_curve.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        days = [date.fromisoformat(row["date"]) for row in rows]
        navs = [float(result.request.initial_cash)] + [float(row["nav"]) for row in rows]
        benchmark = [
            float(value) for _, value in provider.benchmark_series(result.request.benchmark or "", days)
        ]
        active = [
            (navs[index] / navs[index - 1] - 1) - (benchmark[index] / benchmark[index - 1] - 1)
            for index in range(1, len(navs))
        ]
        dispersion = statistics.stdev(active) if len(active) > 1 else 0.0
        return {
            "annualized_tracking_error": dispersion * math.sqrt(252),
            "information_ratio": statistics.fmean(active) / dispersion * math.sqrt(252)
            if dispersion
            else 0.0,
        }
