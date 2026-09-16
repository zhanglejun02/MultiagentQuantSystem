from __future__ import annotations

from pathlib import Path

import pytest

from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.fixtures import generate_fixture_data
from quant_system.infra.db import create_all, make_engine, make_session_factory
from quant_system.rules.a_share import ConfigAShareRuleBook

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory: pytest.TempPathFactory) -> str:
    dest = tmp_path_factory.mktemp("fixtures")
    return generate_fixture_data(str(dest), seed=42)


@pytest.fixture(scope="session")
def provider(fixture_dir: str) -> LocalParquetProvider:
    return LocalParquetProvider(fixture_dir)


@pytest.fixture(scope="session")
def rulebook() -> ConfigAShareRuleBook:
    return ConfigAShareRuleBook(str(ROOT / "configs" / "markets" / "cn_a.yaml"))


@pytest.fixture()
def session_factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    create_all(engine)
    return make_session_factory(engine)


@pytest.fixture()
def registry(session_factory) -> ArtifactRegistry:
    return ArtifactRegistry(session_factory)


@pytest.fixture()
def store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(str(tmp_path / "artifacts"))
