"""Explicit bounded macro collection; credentials are read from the environment only."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.infra.db import create_all, make_engine, make_session_factory
from quant_system.macro import MacroCollectRequest, MacroService
from quant_system.macro.ingestion import load_macro_config
from quant_system.macro.models import DEFAULT_SERIES, SERIES, MacroQualityError

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect official macro observations into immutable artifacts"
    )
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2023, 1, 1))
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--series", nargs="+", choices=sorted(SERIES), default=list(DEFAULT_SERIES))
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "data" / "macro.yaml")
    parser.add_argument(
        "--db-url", default=os.environ.get("QUANT_DB_URL", f"sqlite:///{ROOT / 'var' / 'quant.db'}")
    )
    parser.add_argument(
        "--artifacts-root", default=os.environ.get("QUANT_ARTIFACTS_ROOT", str(ROOT / "var" / "artifacts"))
    )
    parser.add_argument(
        "--canary", action="store_true", help="At most 45 days, current SAFE year only, no full-history sync"
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Analyze the newly frozen snapshot without further network access",
    )
    args = parser.parse_args()
    config = load_macro_config(args.config)
    today = datetime.now(UTC).date()
    end_date = args.end_date or today
    start_date = args.start_date
    if args.canary:
        start_date = max(start_date, today - timedelta(days=45), date(today.year, 1, 1))
        if end_date < start_date or end_date > today:
            parser.error("canary window must be within the current year and most recent 45 days")
        config = config.model_copy(
            update={
                "fred_max_pages": 1,
                "safe_max_years": 1,
                "max_requests": min(config.max_requests, 8),
            }
        )
    request = MacroCollectRequest(series_ids=args.series, start_date=start_date, end_date=end_date)
    (ROOT / "var").mkdir(parents=True, exist_ok=True)
    engine = make_engine(args.db_url)
    if engine.dialect.name == "sqlite":
        create_all(engine)
    service = MacroService(
        LocalArtifactStore(args.artifacts_root),
        ArtifactRegistry(make_session_factory(engine)),
        config=config,
    )
    try:
        result = service.collect(request)
    except MacroQualityError as error:
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "quality_errors": error.quality_errors,
                    "raw_snapshot_id": error.raw_snapshot_id,
                },
                indent=2,
            )
        )
        return 2
    output = {"collection": result.model_dump(mode="json")}
    if args.analyze:
        output["analysis"] = service.analyze(result.snapshot_id)
    print(json.dumps(output, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
