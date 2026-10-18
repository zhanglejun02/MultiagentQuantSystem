"""FastAPI application serving the dashboard.

Thin factory: dependency assembly lives in ``api.container``, routes in
``api.routers.*`` and cross-cutting error mapping in ``api.errors``.
No LLM dependency anywhere: the trading/reporting plane runs standalone.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from quant_system.agents.orchestration import CompletionGateway
from quant_system.api.container import build_container
from quant_system.api.errors import register_exception_handlers
from quant_system.api.research import register_research_routes
from quant_system.api.routers import build_routers
from quant_system.domain.types import utc_now
from quant_system.gold.service import GoldFetcher
from quant_system.infra.settings import Settings
from quant_system.macro.ingestion import MacroTransport


def create_app(
    settings: Settings | None = None,
    *,
    init_schema: bool = False,
    agent_gateway: CompletionGateway | None = None,
    gold_fetcher: GoldFetcher | None = None,
    macro_transport: MacroTransport | None = None,
    research_clock: Callable[[], datetime] = utc_now,
) -> FastAPI:
    container = build_container(
        settings,
        init_schema=init_schema,
        agent_gateway=agent_gateway,
        gold_fetcher=gold_fetcher,
        macro_transport=macro_transport,
        research_clock=research_clock,
    )
    app = FastAPI(title="quant-system", version="0.1.0")
    app.state.research_runtime = container.research_runtime
    register_research_routes(app, container.research_runtime, container.agent_config, container.gateway)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)
    for router in build_routers(container):
        app.include_router(router)
    return app


def factory() -> FastAPI:
    """uvicorn quant_system.api.app:factory --factory"""
    return create_app()
