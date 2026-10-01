"""Fetch, validate and publish one immutable A-share/futures EOD snapshot."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_system.artifacts.registry import ArtifactRegistry  # noqa: E402
from quant_system.artifacts.store import LocalArtifactStore  # noqa: E402
from quant_system.data.ingestion.tushare import (  # noqa: E402
    TushareClient,
    TushareMarketDaySync,
)
from quant_system.data.pipeline import DailyMarketPipeline  # noqa: E402
from quant_system.domain.exceptions import QuantSystemError  # noqa: E402
from quant_system.infra.db import create_all, make_engine, make_session_factory  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish a quality-gated A-share and domestic-futures EOD snapshot."
    )
    parser.add_argument(
        "--trade-date",
        default="auto",
        help="Trading date as YYYY-MM-DD, or auto for the latest open date.",
    )
    parser.add_argument(
        "--assets",
        choices=("equity", "all"),
        default="equity",
        help="equity publishes with the current basic tier; all requires fut_daily.",
    )
    parser.add_argument(
        "--staging-root",
        type=Path,
        default=ROOT / "var" / "data" / "staging" / "daily_market",
    )
    parser.add_argument(
        "--request-interval",
        type=float,
        default=0.13,
        help="Seconds between Tushare requests.",
    )
    parser.add_argument(
        "--expected-equity-symbols",
        type=int,
        default=None,
        help="Optional expected listed-symbol count used by the coverage gate.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "configs" / "data" / "daily_market.yaml",
    )
    args = parser.parse_args()

    config = yaml.safe_load(args.config.read_text())
    sync = TushareMarketDaySync(
        TushareClient(), request_interval=args.request_interval
    )
    if args.trade_date == "auto":
        try:
            trading_day = sync.latest_open_date(date.today())
        except QuantSystemError as exc:
            parser.error(str(exc))
        if trading_day is None:
            raise RuntimeError("no open trading day found in the latest calendar window")
    else:
        trading_day = date.fromisoformat(args.trade_date)

    fetched = sync.sync(
        args.staging_root,
        trade_date=trading_day,
        include_futures=args.assets == "all",
        verify_trade_date=args.trade_date == "auto",
    )
    db_url = os.environ.get("QUANT_DB_URL", f"sqlite:///{ROOT / 'var' / 'quant.db'}")
    engine = make_engine(db_url)
    create_all(engine)
    registry = ArtifactRegistry(make_session_factory(engine))
    store = LocalArtifactStore(str(ROOT / "var" / "artifacts"))
    required_datasets = list(config["datasets"]["required"])
    optional_datasets = list(config["datasets"]["optional"])
    if args.trade_date != "auto":
        required_datasets.remove("trade_calendar")
    if args.assets == "all":
        required_datasets.append("futures_daily")
    result = DailyMarketPipeline(store, registry).publish(
        trade_date=trading_day,
        data_dir=fetched.output_dir,
        datasets=fetched.datasets,
        required_datasets=required_datasets,
        optional_datasets=optional_datasets,
        acquisition_errors=fetched.acquisition_errors,
        expected_equity_symbols=args.expected_equity_symbols,
        minimum_equity_coverage=float(config["quality"]["minimum_equity_coverage"]),
    )
    payload = {
        "fetch": {
            **asdict(fetched),
            "datasets": {
                name: len(rows) for name, rows in sorted(fetched.datasets.items())
            },
        },
        "publish": {
            "passed": result.passed,
            "quality_report_id": result.quality_report.artifact_id,
            "data_snapshot_id": (
                result.data_snapshot.artifact_id if result.data_snapshot else None
            ),
            "market_snapshot_id": (
                result.market_snapshot.artifact_id if result.market_snapshot else None
            ),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if not result.passed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()