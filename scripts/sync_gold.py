"""Publish a frozen, read-only gold observation snapshot from public sources."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_system.artifacts.registry import ArtifactRegistry  # noqa: E402
from quant_system.artifacts.store import LocalArtifactStore  # noqa: E402
from quant_system.gold.service import GoldService  # noqa: E402
from quant_system.infra.db import create_all, make_engine, make_session_factory  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="Latest included date (today after 18:00 Asia/Shanghai, otherwise yesterday).",
    )
    args = parser.parse_args()
    engine = make_engine(os.environ.get("QUANT_DB_URL", f"sqlite:///{ROOT / 'var' / 'quant.db'}"))
    create_all(engine)
    service = GoldService(
        LocalArtifactStore(os.environ.get("QUANT_ARTIFACTS_ROOT", str(ROOT / "var" / "artifacts"))),
        ArtifactRegistry(make_session_factory(engine)),
    )
    try:
        artifact, snapshot = service.refresh(as_of=args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "artifact_id": artifact.artifact_id,
                "as_of": max(quote.points[-1].date for quote in snapshot.quotes).isoformat(),
                "quotes": [
                    {
                        "symbol": quote.symbol,
                        "observations": len(quote.points),
                        "latest_date": quote.points[-1].date.isoformat(),
                    }
                    for quote in snapshot.quotes
                ],
                "warnings": snapshot.warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
