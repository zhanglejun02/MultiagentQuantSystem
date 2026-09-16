"""FastAPI application serving the dashboard.

Read paths come from the artifact registry and persisted backtest run files.
No LLM dependency anywhere: the trading/reporting plane runs standalone.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from quant_system.artifacts.lineage import ArtifactLineageService
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.domain.enums import ArtifactType, ExperimentState, FactorState
from quant_system.domain.exceptions import (
    ArtifactNotFoundError,
    InvalidTransitionError,
    QuantSystemError,
)
from quant_system.domain.types import utc_now
from quant_system.factors.evaluation import FactorLab
from quant_system.infra.db import create_all, make_engine, make_session_factory
from quant_system.infra.settings import Settings
from quant_system.research.experiments import ExperimentService
from quant_system.research.factors import FactorRegistry
from quant_system.risk.runtime import KillSwitch, TradingRuntime


class ExperimentCreate(BaseModel):
    name: str
    description: str = ""


class TransitionRequest(BaseModel):
    target: ExperimentState
    reason: str = ""
    actor: str = "dashboard"


class FactorCreate(BaseModel):
    factor_key: str
    version: str
    name: str
    category: str
    description: str = ""
    owner: str
    code_ref: str
    data_snapshot_id: str | None = None
    validation_artifact_id: str | None = None
    parameters: dict[str, Any] = {}
    metrics: dict[str, Any] = {}


class FactorTransitionRequest(BaseModel):
    target: FactorState
    reason: str = ""
    actor: str = "dashboard"


class FactorEvaluateRequest(BaseModel):
    expression: str
    universe_id: str = "cn_a_all"
    start_date: date
    end_date: date
    target: str = "NEXT_OPEN_TO_CLOSE"
    horizon: int = 1


def _run_dir(uri: str) -> Path:
    if not uri.startswith("file://"):
        raise HTTPException(404, f"unsupported artifact uri {uri}")
    p = Path(uri[len("file://"):])
    if not p.exists():
        raise HTTPException(404, f"artifact content missing at {p}")
    return p


def _read_jsonl(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
            if len(out) >= limit:
                break
    return out


def create_app(settings: Settings | None = None, *, init_schema: bool = False) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.db_url)
    if init_schema:
        create_all(engine)
    session_factory = make_session_factory(engine)
    registry = ArtifactRegistry(session_factory)
    lineage = ArtifactLineageService(registry)
    experiments = ExperimentService(session_factory)
    factors = FactorRegistry(session_factory)
    factor_lab = FactorLab(session_factory, LocalParquetProvider(settings.fixtures_root))
    kill_switch = KillSwitch()
    runtime = TradingRuntime(kill_switch)

    app = FastAPI(title="quant-system", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _env_to_dict(env) -> dict[str, Any]:
        return env.model_dump(mode="json")

    @app.get("/api/health")
    def health() -> dict:
        db_ok = True
        try:
            registry.find_by_type(ArtifactType.DATA_SNAPSHOT)
        except Exception:
            db_ok = False
        return {
            "status": "ok" if db_ok else "degraded",
            "db": "ok" if db_ok else "unreachable",
            "time": utc_now().isoformat(),
        }

    # -- artifacts ------------------------------------------------------------
    @app.get("/api/artifacts")
    def list_artifacts(artifact_type: str | None = Query(default=None)) -> list[dict]:
        if artifact_type:
            envs = registry.find_by_type(ArtifactType(artifact_type))
        else:
            envs = registry.list_all()
        return [_env_to_dict(e) for e in envs]

    @app.get("/api/artifacts/{artifact_id}")
    def get_artifact(artifact_id: str) -> dict:
        try:
            return _env_to_dict(registry.get(artifact_id))
        except ArtifactNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/artifacts/{artifact_id}/lineage")
    def get_lineage(artifact_id: str) -> dict:
        try:
            graph = lineage.lineage(artifact_id)
        except ArtifactNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {
            "ancestors": [_env_to_dict(a) for a in graph["ancestors"]],
            "descendants": [_env_to_dict(d) for d in graph["descendants"]],
        }

    # -- experiments -----------------------------------------------------------
    @app.get("/api/experiments")
    def list_experiments() -> list[dict]:
        return [e.model_dump(mode="json") for e in experiments.list_all()]

    @app.post("/api/experiments")
    def create_experiment(body: ExperimentCreate) -> dict:
        return experiments.create(body.name, body.description).model_dump(mode="json")

    @app.post("/api/experiments/{experiment_id}/transition")
    def transition_experiment(experiment_id: str, body: TransitionRequest) -> dict:
        try:
            exp = experiments.transition(
                experiment_id, body.target, reason=body.reason, actor=body.actor
            )
        except InvalidTransitionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except QuantSystemError as exc:
            raise HTTPException(404, str(exc)) from exc
        return exp.model_dump(mode="json")

    @app.get("/api/experiments/{experiment_id}/history")
    def experiment_history(experiment_id: str) -> list[dict]:
        return experiments.history(experiment_id)

    @app.get("/api/experiments/{experiment_id}/runs")
    def experiment_runs(experiment_id: str) -> list[dict]:
        return [r.model_dump(mode="json") for r in experiments.runs(experiment_id)]

    # -- factors ---------------------------------------------------------------
    @app.get("/api/factors")
    def list_factors() -> list[dict]:
        return [factor.model_dump(mode="json") for factor in factors.list_all()]

    @app.post("/api/factors", status_code=201)
    def create_factor(body: FactorCreate) -> dict:
        try:
            factor = factors.create(**body.model_dump())
        except QuantSystemError as exc:
            raise HTTPException(409, str(exc)) from exc
        return factor.model_dump(mode="json")

    @app.post("/api/factors/{factor_id}/transition")
    def transition_factor(factor_id: str, body: FactorTransitionRequest) -> dict:
        try:
            factor = factors.transition(
                factor_id, body.target, reason=body.reason, actor=body.actor
            )
        except InvalidTransitionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except QuantSystemError as exc:
            raise HTTPException(404, str(exc)) from exc
        return factor.model_dump(mode="json")

    @app.get("/api/factors/{factor_id}/history")
    def factor_history(factor_id: str) -> list[dict]:
        return factors.history(factor_id)

    @app.get("/api/factor-runs")
    def list_factor_runs() -> list[dict]:
        return [run.model_dump(mode="json") for run in factor_lab.list_runs()]

    @app.post("/api/factor-runs/evaluate", status_code=201)
    def evaluate_factor(body: FactorEvaluateRequest) -> dict:
        try:
            run = factor_lab.evaluate(**body.model_dump())
        except (QuantSystemError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return run.model_dump(mode="json")

    # -- backtests ---------------------------------------------------------------
    @app.get("/api/backtests")
    def list_backtests() -> list[dict]:
        envs = registry.find_by_type(ArtifactType.BACKTEST_RESULT)
        return sorted(
            (_env_to_dict(e) for e in envs), key=lambda d: d["created_at"], reverse=True
        )

    def _backtest_dir(artifact_id: str) -> Path:
        try:
            env = registry.get(artifact_id)
        except ArtifactNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        if env.artifact_type != ArtifactType.BACKTEST_RESULT:
            raise HTTPException(400, f"{artifact_id} is not a backtest result")
        return _run_dir(env.uri)

    @app.get("/api/backtests/{artifact_id}/metrics")
    def backtest_metrics(artifact_id: str) -> dict:
        path = _backtest_dir(artifact_id) / "metrics.json"
        return json.loads(path.read_text())

    @app.get("/api/backtests/{artifact_id}/equity")
    def backtest_equity(artifact_id: str) -> list[dict]:
        path = _backtest_dir(artifact_id) / "equity_curve.csv"
        lines = path.read_text().strip().splitlines()
        header = lines[0].split(",")
        return [dict(zip(header, line.split(","), strict=True)) for line in lines[1:]]

    @app.get("/api/backtests/{artifact_id}/orders")
    def backtest_orders(artifact_id: str, limit: int = 500) -> list[dict]:
        return _read_jsonl(_backtest_dir(artifact_id) / "orders.jsonl", limit)

    @app.get("/api/backtests/{artifact_id}/trades")
    def backtest_trades(artifact_id: str, limit: int = 500) -> list[dict]:
        return _read_jsonl(_backtest_dir(artifact_id) / "trades.jsonl", limit)

    @app.get("/api/backtests/{artifact_id}/events")
    def backtest_events(
        artifact_id: str, event_type: str | None = None, limit: int = 1000
    ) -> list[dict]:
        rows = _read_jsonl(_backtest_dir(artifact_id) / "events.jsonl", 100_000)
        if event_type:
            rows = [r for r in rows if r.get("event_type") == event_type]
        return rows[:limit]

    @app.get("/api/backtests/{artifact_id}/positions")
    def backtest_positions(artifact_id: str) -> dict:
        path = _backtest_dir(artifact_id) / "positions.json"
        return json.loads(path.read_text())

    # -- portfolio (latest backtest as paper account) ------------------------------
    @app.get("/api/portfolio/latest")
    def latest_portfolio() -> dict:
        envs = registry.find_by_type(ArtifactType.BACKTEST_RESULT)
        if not envs:
            raise HTTPException(404, "no backtest results")
        latest = max(envs, key=lambda e: e.created_at)
        path = _run_dir(latest.uri) / "positions.json"
        doc = json.loads(path.read_text())
        doc["source_artifact_id"] = latest.artifact_id
        return doc

    # -- system / risk ---------------------------------------------------------
    @app.get("/api/system/status")
    def system_status() -> dict:
        snapshots = registry.find_by_type(ArtifactType.DATA_SNAPSHOT)
        results = registry.find_by_type(ArtifactType.BACKTEST_RESULT)
        return {
            "runtime_state": runtime.state.value,
            "kill_switch": kill_switch.engaged,
            "kill_switch_reason": kill_switch.reason,
            "safe_mode_reason": runtime.safe_mode_reason,
            "n_data_snapshots": len(snapshots),
            "n_backtest_results": len(results),
            "paper_broker": "SIMULATED",
            "oms": "IDLE",
            "heartbeat": utc_now().isoformat(),
        }

    @app.post("/api/system/killswitch")
    def toggle_killswitch(engage: bool, reason: str = "manual", operator: str = "dashboard") -> dict:
        if engage:
            kill_switch.engage(reason)
        else:
            kill_switch.disengage(operator)
        return {"kill_switch": kill_switch.engaged, "reason": kill_switch.reason}

    return app


def factory() -> FastAPI:
    """uvicorn quant_system.api.app:factory --factory"""
    return create_app()
