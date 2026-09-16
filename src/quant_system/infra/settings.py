from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_url: str
    artifacts_root: str
    configs_root: str
    fixtures_root: str = ""

    @staticmethod
    def from_env(base_dir: str | None = None) -> Settings:
        base = base_dir or os.getcwd()
        return Settings(
            db_url=os.environ.get(
                "QUANT_DB_URL", "postgresql+psycopg://quant:quant@localhost:5433/quant"
            ),
            artifacts_root=os.environ.get("QUANT_ARTIFACTS_ROOT", os.path.join(base, "var", "artifacts")),
            configs_root=os.environ.get("QUANT_CONFIGS_ROOT", os.path.join(base, "configs")),
            fixtures_root=os.environ.get(
                "QUANT_FIXTURES_ROOT", os.path.join(base, "var", "fixtures", "cn_a")
            ),
        )
