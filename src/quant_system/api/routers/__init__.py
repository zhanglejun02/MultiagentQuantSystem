"""Domain routers for the dashboard API.

Each module exposes ``build_router(container) -> APIRouter``; the app factory
mounts them all. Cross-cutting error mapping lives in ``api.errors``.
"""

from __future__ import annotations

from fastapi import APIRouter

from quant_system.api.container import ApiContainer
from quant_system.api.routers import (
    agent_research,
    artifacts,
    backtests,
    experiments,
    factors,
    futures,
    gold,
    market,
    strategies,
    system,
)


def build_routers(container: ApiContainer) -> list[APIRouter]:
    return [
        system.build_router(container),
        artifacts.build_router(container),
        market.build_router(container),
        gold.build_router(container),
        experiments.build_router(container),
        agent_research.build_router(container),
        factors.build_router(container),
        strategies.build_router(container),
        backtests.build_router(container),
        futures.build_router(container),
    ]
