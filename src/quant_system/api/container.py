"""Dependency assembly for the dashboard API.

Builds every service once at startup; routers receive this container and never
construct infrastructure themselves.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from quant_system.agents.config import AgentLLMConfig, load_agent_llm_config
from quant_system.agents.llm import LLMGateway
from quant_system.agents.orchestration import (
    AgentResearchOrchestrator,
    CompletionGateway,
)
from quant_system.api.backtest_files import BacktestResultReader
from quant_system.artifacts.lineage import ArtifactLineageService
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.domain.enums import ArtifactType
from quant_system.domain.types import utc_now
from quant_system.factors.evaluation import FactorLab
from quant_system.futures.backtest import FuturesBacktestEngine
from quant_system.futures.contracts import FuturesContractCatalog
from quant_system.futures.data import FixtureFuturesData
from quant_system.futures.repository import FuturesSimulationRepository
from quant_system.futures.risk import FuturesPreTradeRisk
from quant_system.futures.service import FuturesSimulationService
from quant_system.gold.research import GoldResearchService
from quant_system.gold.service import GoldFetcher, GoldService
from quant_system.infra.db import create_all, make_engine, make_session_factory
from quant_system.infra.settings import Settings
from quant_system.macro import MacroService
from quant_system.macro.ingestion import MacroTransport, load_macro_config
from quant_system.research.experiments import ExperimentService
from quant_system.research.factor_promotion import FactorPromotionService
from quant_system.research.factors import FactorRegistry
from quant_system.research.modules.config import apply_task_profiles, load_task_profiles
from quant_system.research.modules.factor import factor_tasks
from quant_system.research.modules.gold import gold_tasks
from quant_system.research.modules.macro import macro_gold_facts, macro_tasks, macro_tools
from quant_system.research.modules.recipes import register_research_recipes
from quant_system.research.runtime.engine import ResearchRuntime
from quant_system.research.runtime.ports import ModuleRegistry
from quant_system.research.runtime.storage import ArtifactRunRepository
from quant_system.research.strategies import StrategyPromotionService, StrategyRegistry
from quant_system.risk.portfolio import PortfolioRiskControlPlane
from quant_system.risk.runtime import KillSwitch, TradingRuntime


@dataclass(frozen=True)
class ApiContainer:
    settings: Settings
    registry: ArtifactRegistry
    artifact_store: LocalArtifactStore
    lineage: ArtifactLineageService
    gold: GoldService
    experiments: ExperimentService
    factors: FactorRegistry
    factor_promotion: FactorPromotionService
    factor_lab: FactorLab
    agent_config: AgentLLMConfig
    gateway: CompletionGateway
    research_runtime: ResearchRuntime
    gold_research: GoldResearchService
    kill_switch: KillSwitch
    trading_runtime: TradingRuntime
    portfolio_risk: PortfolioRiskControlPlane
    strategies: StrategyRegistry
    strategy_promotion: StrategyPromotionService
    futures: FuturesSimulationService
    futures_backtest_engine: FuturesBacktestEngine
    backtest_reader: BacktestResultReader
    futures_backtest_reader: BacktestResultReader


def build_container(
    settings: Settings | None = None,
    *,
    init_schema: bool = False,
    agent_gateway: CompletionGateway | None = None,
    gold_fetcher: GoldFetcher | None = None,
    macro_transport: MacroTransport | None = None,
    research_clock: Callable[[], datetime] = utc_now,
) -> ApiContainer:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.db_url)
    if init_schema:
        create_all(engine)
    session_factory = make_session_factory(engine)
    registry = ArtifactRegistry(session_factory)
    artifact_store = LocalArtifactStore(settings.artifacts_root)
    lineage = ArtifactLineageService(registry)
    gold = GoldService(artifact_store, registry)
    experiments = ExperimentService(session_factory)
    factors = FactorRegistry(session_factory)
    factor_promotion = FactorPromotionService(factors, registry)
    factor_lab = FactorLab(session_factory, LocalParquetProvider(settings.fixtures_root))
    agent_config = load_agent_llm_config(Path(settings.configs_root) / "agents" / "default.yaml")
    gateway = agent_gateway or LLMGateway(agent_config)
    agent_research = AgentResearchOrchestrator(
        config=agent_config,
        gateway=gateway,
        experiments=experiments,
        factor_lab=factor_lab,
        registry=registry,
        store=artifact_store,
        factor_lab_factory=lambda provider: FactorLab(session_factory, provider),
    )
    macro_config_path = Path(settings.configs_root) / "data" / "macro.yaml"
    macro = MacroService(
        artifact_store,
        registry,
        clock=research_clock,
        transport=macro_transport,
        config=load_macro_config(macro_config_path) if macro_config_path.exists() else None,
    )

    def read_macro(snapshot_id: str, cutoff: datetime) -> dict[str, Any]:
        return macro_gold_facts(macro, snapshot_id, cutoff, store=artifact_store, registry=registry)

    modules = ModuleRegistry()
    tasks = [
        *gold_tasks(gold, agent_config, fetcher=gold_fetcher, macro_evidence_reader=read_macro),
        *factor_tasks(agent_research),
        *macro_tasks(macro, agent_config, store=artifact_store, registry=registry),
    ]
    profiles = load_task_profiles(Path(settings.configs_root) / "research" / "tasks.yaml")
    for task in apply_task_profiles(tasks, profiles, agent_config):
        modules.register(task)
    register_research_recipes(modules)
    research_runtime = ResearchRuntime(
        modules,
        ArtifactRunRepository(artifact_store, registry, clock=research_clock),
        agent_config,
        gateway,
        clock=research_clock,
        credentials_required=agent_gateway is None,
    )
    for tool in macro_tools(macro):
        research_runtime.tools.register(tool)
    gold_research = GoldResearchService(
        gold,
        agent_config,
        gateway,
        credentials_required=agent_gateway is None,
        fetcher=gold_fetcher,
        clock=research_clock,
        runtime=research_runtime,
        macro_evidence_reader=read_macro,
    )
    kill_switch = KillSwitch()
    trading_runtime = TradingRuntime(kill_switch)
    portfolio_risk = PortfolioRiskControlPlane()
    strategies = StrategyRegistry(session_factory)
    strategy_promotion = StrategyPromotionService(strategies, factors, registry, trading_runtime)
    futures_repository = FuturesSimulationRepository(session_factory)
    futures_catalog = FuturesContractCatalog.from_yaml(
        Path(settings.configs_root) / "futures" / "contracts.yaml"
    )
    futures = FuturesSimulationService(
        futures_repository,
        futures_catalog,
        FixtureFuturesData(),
        trading_runtime,
        risk=FuturesPreTradeRisk(trading_runtime, portfolio_risk=portfolio_risk),
    )
    futures_backtest_engine = FuturesBacktestEngine(
        catalog=futures_catalog,
        data=FixtureFuturesData(),
        store=artifact_store,
        registry=registry,
    )
    return ApiContainer(
        settings=settings,
        registry=registry,
        artifact_store=artifact_store,
        lineage=lineage,
        gold=gold,
        experiments=experiments,
        factors=factors,
        factor_promotion=factor_promotion,
        factor_lab=factor_lab,
        agent_config=agent_config,
        gateway=gateway,
        research_runtime=research_runtime,
        gold_research=gold_research,
        kill_switch=kill_switch,
        trading_runtime=trading_runtime,
        portfolio_risk=portfolio_risk,
        strategies=strategies,
        strategy_promotion=strategy_promotion,
        futures=futures,
        futures_backtest_engine=futures_backtest_engine,
        backtest_reader=BacktestResultReader(registry, ArtifactType.BACKTEST_RESULT, "backtest result"),
        futures_backtest_reader=BacktestResultReader(
            registry, ArtifactType.FUTURES_BACKTEST_RESULT, "futures backtest result"
        ),
    )
