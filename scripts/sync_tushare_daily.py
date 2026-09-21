"""Synchronize Tushare Pro daily data into the local PIT dataset."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from quant_system.data.ingestion.tushare import TushareClient, TushareDailySync, result_as_json


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync unadjusted SSE/SZSE daily bars from Tushare Pro."
    )
    parser.add_argument("--start", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("var/data/tushare/cn_a_daily"),
        help="PIT dataset directory",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace bars instead of merging by symbol and trade date",
    )
    parser.add_argument(
        "--request-interval",
        type=float,
        default=0.13,
        help="Seconds between daily API calls",
    )
    args = parser.parse_args()

    result = TushareDailySync(
        TushareClient(), request_interval=args.request_interval
    ).sync(
        args.output,
        start=args.start,
        end=args.end,
        overwrite=args.overwrite,
    )
    print(result_as_json(result))


if __name__ == "__main__":
    main()