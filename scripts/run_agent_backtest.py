"""Run one real four-agent candidate discovery followed by frozen-data backtests."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import shutil
import tempfile
from datetime import date
from pathlib import Path
from threading import Lock

from run_baseline_research import code_identity

from quant_system.agents.config import AgentRoleName, load_agent_llm_config
from quant_system.agents.llm import CompletionGateway, LLMGateway, LLMMessage, LLMResponse
from quant_system.agents.orchestration import AgentResearchOrchestrator, AgentResearchResult
from quant_system.artifacts.envelope import hash_bytes, hash_json
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.artifacts.types import research_report_artifact
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.data.snapshots.service import DataSnapshotService
from quant_system.domain.enums import ArtifactStatus
from quant_system.domain.exceptions import QuantSystemError
from quant_system.domain.types import new_id
from quant_system.factors.evaluation import FactorLab
from quant_system.infra.db import create_all, make_engine, make_session_factory
from quant_system.research.baseline.runner import BaselineRunner
from quant_system.research.baseline.spec import BaselineSpec
from quant_system.research.experiments import ExperimentService
from quant_system.research.modules.factor import FactorTaskRequest, factor_tasks
from quant_system.research.runtime.engine import ResearchRuntime
from quant_system.research.runtime.models import RunRequest
from quant_system.research.runtime.ports import ModuleRegistry
from quant_system.research.runtime.storage import ArtifactRunRepository

ROOT = Path(__file__).resolve().parents[1]


class DevelopmentProvider(LocalParquetProvider):
    def __init__(self, data_dir: str, end: date):
        super().__init__(data_dir)
        self.end = end

    def get_bars(self, symbols, start, end, *, as_of):
        return super().get_bars(symbols, start, min(end, self.end), as_of=as_of)

    def trading_calendar(self, start, end):
        return super().trading_calendar(start, min(end, self.end))


class RecordedGateway:
    def __init__(self, gateway: CompletionGateway, directory: Path):
        self.gateway = gateway
        self.directory = directory
        self.count = 0
        self._lock = Lock()

    def complete(self, role: AgentRoleName, messages: list[LLMMessage]) -> LLMResponse:
        with self._lock:
            self.count += 1
            index = self.count
        path = self.directory / f"{index:02d}_{role}.json"
        request = {"role": role, "messages": [message.model_dump(mode="json") for message in messages]}
        path.write_text(json.dumps(request, ensure_ascii=False, indent=2))
        print(f"LLM call {index}: {role} started", flush=True)
        try:
            response = self.gateway.complete(role, messages)
        except Exception:
            print(f"LLM call {index}: {role} failed; request retained", flush=True)
            raise
        path.write_text(
            json.dumps(
                {**request, "response": response.model_dump(mode="json")}, ensure_ascii=False, indent=2
            )
        )
        print(f"LLM call {index}: {role} completed ({response.provider}/{response.model})", flush=True)
        return response


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/research/a_share_baseline.yaml")
    parser.add_argument("--agent-config", type=Path, default=ROOT / "configs/agents/default.yaml")
    parser.add_argument("--workspace", type=Path, default=ROOT / "var/research/a_share_baseline")
    args = parser.parse_args()
    spec = BaselineSpec.load(args.config)
    config = load_agent_llm_config(args.agent_config)
    for role in ("researcher", "critic", "statistician", "orchestrator"):
        provider, _ = config.route(role)
        provider.api_key()
    roles = {
        role: route.model_copy(
            update={
                "max_output_tokens": min(route.max_output_tokens, 2048),
            }
        )
        for role, route in config.roles.items()
    }
    config = config.model_copy(
        update={
            "roles": roles,
            "research_budget": config.research_budget.model_copy(
                update={
                    "max_candidates_per_run": 1,
                    "max_llm_calls_per_run": min(config.research_budget.max_llm_calls_per_run, 8),
                    "max_total_tokens_per_run": min(config.research_budget.max_total_tokens_per_run, 16000),
                }
            ),
        }
    )
    workspace = args.workspace.resolve()
    database = make_engine(f"sqlite:///{workspace / 'registry.db'}")
    create_all(database)
    sessions = make_session_factory(database)
    registry = ArtifactRegistry(sessions)
    store = LocalArtifactStore(str(workspace / "artifacts"))
    snapshots = DataSnapshotService(store, registry)
    frozen = snapshots.resolve_path(args.snapshot_id)
    metadata = json.loads((frozen / "dataset_metadata.json").read_text())
    if metadata.get("test_fixture") or metadata.get("protocol_hash") != hash_json(
        spec.model_dump(mode="json")
    ):
        raise ValueError("agent discovery requires the matching real frozen dataset")
    directory = workspace / "agent_runs" / new_id("workflow_")
    directory.mkdir(parents=True)
    commit, _, paths = code_identity()
    paths = [*paths, Path(__file__).resolve()]
    code_hash = hash_json({str(path.relative_to(ROOT)): hash_bytes(path.read_bytes()) for path in paths})
    development = spec.segments[0]
    request = FactorTaskRequest(
        topic=(
            "提出一个可解释的A股量价因子候选，最多20日窗口且不嵌套时间函数，horizon固定5。"
            "只有8只当今选择的沪市主板股票，存在选择/幸存者偏差和未认证历史ST；价格为PIT现金分红复权，"
            "成交为原始价。每周首日收盘按分数降序Top3等权、90%仓位、次日开盘执行。"
            "只允许一个候选，不能根据后续结果改参数，不宣称可部署。无成交量单位复权，禁止未来信息。"
        ),
        start_date=development.start,
        end_date=development.end,
        target="NEXT_OPEN_TO_NEXT_OPEN",
        data_mode="frozen_snapshot",
        data_snapshot_id=args.snapshot_id,
    )
    context = {
        "request": request.model_dump(mode="json"),
        "protocol": spec.model_dump(mode="json"),
        "agent_config": config.model_dump(mode="json"),
        "code_hash": code_hash,
        "candidate_budget": 1,
        "research_only": True,
        "deployment_eligible": False,
        "notes": [
            "Validation/holdout data unavailable to discovery.",
            "Historical holdout has been inspected in prior baseline; not prospective unseen data.",
            "Model fees are not estimated; call/token limits are enforced.",
        ],
    }
    (directory / "context.json").write_text(json.dumps(context, ensure_ascii=False, indent=2))
    gateway = LLMGateway(config)
    orchestrator = AgentResearchOrchestrator(
        config=config,
        gateway=gateway,
        experiments=ExperimentService(sessions),
        factor_lab=FactorLab(
            sessions, DevelopmentProvider(str(frozen), development.end), adjusted_features=True
        ),
        registry=registry,
        code_commit=commit,
        store=store,
        snapshot_service=snapshots,
    )
    modules = ModuleRegistry()
    for task in factor_tasks(orchestrator):
        modules.register(task)
    runtime = ResearchRuntime(modules, ArtifactRunRepository(store, registry), config, gateway)
    submitted = runtime.submit(
        RunRequest(task_type="factor.research", payload=request.model_dump(mode="json"))
    )
    print(f"Factor research run {submitted.run_id} started", flush=True)
    runtime.execute(submitted.run_id)
    run = runtime.get(submitted.run_id)
    (directory / "runtime.json").write_text(run.model_dump_json(indent=2))
    for receipt in runtime.calls(submitted.run_id):
        (directory / f"{receipt.sequence:02d}_{receipt.role}.json").write_text(
            receipt.model_dump_json(indent=2)
        )
        print(f"LLM call {receipt.sequence}: {receipt.role} {receipt.status.lower()}", flush=True)
    if run.status != "COMPLETED" or run.result is None:
        raise QuantSystemError(run.error or f"factor research {run.status.lower()}")
    result = AgentResearchResult.model_validate(run.result.payload)
    (directory / "discovery.json").write_text(result.model_dump_json(indent=2))
    print(f"Candidate: {result.candidate.name}: {result.candidate.expression}", flush=True)
    print(
        f"Discovery verdict: {result.experiment.state.value}; audited backtest runs even if rejected",
        flush=True,
    )
    uri, digest = store.put_content_addressed_dir("research/agent_discovery", str(directory))
    evidence = registry.register(
        research_report_artifact(
            producer="agent-discovery-capture",
            uri=uri,
            content_hash=digest,
            code_commit=commit,
            data_snapshot_id=args.snapshot_id,
            experiment_id=result.experiment.experiment_id,
            parents=(args.snapshot_id, result.report_artifact_id),
            status=ArtifactStatus.VERIFIED,
            config=context,
            metadata={
                "module_id": "factor",
                "task_type": "factor.research",
                "runtime_run_id": run.run_id,
                "research_only": True,
                "deployment_eligible": False,
                "candidate": result.candidate.model_dump(mode="json"),
            },
        )
    )
    environment = {
        "python": platform.python_version(),
        **{
            package: importlib.metadata.version(package)
            for package in ("pydantic", "pyarrow", "SQLAlchemy", "PyYAML", "pycurl")
        },
    }
    with tempfile.TemporaryDirectory() as temporary:
        bundle = Path(temporary)
        for path in paths:
            destination = bundle / path.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        (bundle / "agent_workflow.json").write_text(json.dumps(context, ensure_ascii=False, indent=2))
        report = BaselineRunner(
            store=store,
            registry=registry,
            experiments=ExperimentService(sessions),
            rulebook_path=ROOT / "configs/markets/cn_a.yaml",
            code_commit=commit,
            code_hash=code_hash,
            environment=environment,
            code_bundle_path=bundle,
        ).run(
            spec,
            args.snapshot_id,
            strategy_expression=result.candidate.expression,
            research_evidence_ids=(evidence.artifact_id,),
        )
    output = {
        "research_run_id": run.run_id,
        "candidate": result.candidate.model_dump(mode="json"),
        "llm_calls": len(result.calls),
        "discovery_state": result.experiment.state.value,
        "evidence_artifact_id": evidence.artifact_id,
        "evidence_uri": evidence.uri,
        "backtest_report_id": report.artifact_id,
        "backtest_report_uri": report.uri,
        "deployment_eligible": False,
    }
    (directory / "result.json").write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
