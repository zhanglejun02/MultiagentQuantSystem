"""Cached vendor acquisition and strict normalization for a small research basket."""

import json
import shutil
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from quant_system.artifacts.envelope import hash_json
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.data.ingestion.corporate_actions import (
    cash_adjustment_factors,
    cash_dividends,
    restore_unadjusted_reference,
    vendor_date,
)
from quant_system.data.ingestion.eastmoney import fetch_cash_dividends, fetch_company_master
from quant_system.data.ingestion.tencent import fetch_index_history
from quant_system.data.ingestion.tushare import (
    ADJ_FACTOR_FIELDS,
    DAILY_FIELDS,
    INDEX_DAILY_FIELDS,
    NAMECHANGE_FIELDS,
    SECURITY_FIELDS,
    TushareClient,
    normalize_equity_bar,
)
from quant_system.domain.exceptions import QuantSystemError
from quant_system.domain.types import bar_available_at, cn_dt

DIVIDEND_FIELDS = "ts_code,ann_date,div_proc,stk_div,cash_div_tax,ex_date,pay_date"


class BaselineDatasetBuilder:
    def __init__(self, client: TushareClient, raw_root: Path, request_interval: float = 0.35):
        if request_interval < 0:
            raise ValueError("request_interval must be non-negative")
        self.client = client
        self.raw_root = raw_root
        self.request_interval = request_interval
        self.raw_manifest: dict[str, str] = {}
        self._last_call = 0.0

    def _query(self, api: str, params: dict[str, Any], fields: str) -> list[dict[str, Any]]:
        request = {"api": api, "params": params, "fields": fields}

        def fetch() -> list[dict[str, Any]]:
            remaining = self.request_interval - (time.monotonic() - self._last_call)
            if remaining > 0:
                time.sleep(remaining)
            rows = self.client.query(api, params=params, fields=fields)
            self._last_call = time.monotonic()
            return rows

        return self._cached_response(api, request, fetch)

    def _cached_response(
        self, namespace: str, request: dict[str, Any], fetch: Callable[[], list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        key = hash_json(request)
        relative = f"{namespace}/{key}.json"
        path = self.raw_root / relative
        if path.exists():
            payload = json.loads(path.read_text())
            if payload["response_hash"] != hash_json(payload["rows"]):
                raise QuantSystemError(f"raw response integrity failed: {relative}")
        else:
            rows = fetch()
            payload = {
                **request,
                "source": namespace,
                "rows": rows,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "response_hash": hash_json(rows),
            }
            LocalArtifactStore(str(self.raw_root)).put_json(relative, payload)
        self.raw_manifest[relative] = payload["response_hash"]
        return payload["rows"]

    def prepare(
        self,
        destination: Path,
        *,
        symbols: tuple[str, ...],
        benchmark: str,
        start: date,
        end: date,
        protocol_hash: str,
        benchmark_source: str = "tushare",
        reference_source: str = "tushare",
        adjustment_source: str = "tushare",
        dividend_source: str = "tushare",
    ) -> dict[str, Any]:
        if destination.exists():
            raise QuantSystemError(
                "prepared dataset already exists; freeze/reuse it or choose a new destination"
            )
        self.raw_manifest = {}
        params = {"start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")}
        if benchmark_source == "tushare":
            index_rows = self._query("index_daily", {"ts_code": benchmark, **params}, INDEX_DAILY_FIELDS)
        elif benchmark_source == "tencent":
            index_rows = self._cached_response(
                "tencent_index",
                {"symbol": benchmark, "start": start, "end": end},
                lambda: fetch_index_history(benchmark, start, end),
            )
        else:
            raise ValueError("unknown benchmark source")
        days = sorted(vendor_date(row["trade_date"]) for row in index_rows)
        if not days or len(set(days)) != len(days) or not start <= days[0] <= days[-1] <= end:
            raise QuantSystemError("benchmark calendar is empty, duplicated or outside requested range")
        if (end - days[-1]).days > 7:
            raise QuantSystemError("benchmark history is truncated")
        bars: list[dict] = []
        factors: list[dict] = []
        securities: list[dict] = []
        statuses: list[dict] = []
        actions: list[dict] = []
        reconciliations: list[dict] = []
        counts = {}
        if reference_source == "tushare":
            master = self._query("stock_basic", {"exchange": "SSE", "list_status": "L"}, SECURITY_FIELDS)
        elif reference_source == "eastmoney":
            master = self._cached_response(
                "eastmoney_master", {"symbols": sorted(symbols)}, lambda: fetch_company_master(symbols)
            )
        else:
            raise ValueError("unknown reference source")
        public_dividends: list[dict[str, Any]] = []
        if dividend_source == "eastmoney":
            public_dividends = self._cached_response(
                "eastmoney_dividends",
                {"symbols": sorted(symbols), "start": start, "end": end},
                lambda: fetch_cash_dividends(symbols, start, end),
            )
        for code in symbols:
            symbol = code.replace(".SH", ".SSE")
            basic = [row for row in master if row["ts_code"] == code]
            if len(basic) != 1 or basic[0]["market"] != "主板" or basic[0].get("delist_date"):
                raise QuantSystemError(f"unsupported or missing main-board security: {code}")
            listed = vendor_date(basic[0]["list_date"])
            if listed >= start:
                raise QuantSystemError(f"security must predate warmup: {code}")
            securities.append(
                {
                    "symbol": symbol,
                    "name": basic[0]["name"],
                    "exchange": "SSE",
                    "board": "MAIN",
                    "security_type": "STOCK",
                    "list_date": listed.isoformat(),
                    "delist_date": None,
                    "available_at": cn_dt(listed, 9, 0).isoformat(),
                }
            )
            raw_bars = self._query("daily", {"ts_code": code, **params}, DAILY_FIELDS)
            normalized = sorted(
                (normalize_equity_bar(row) for row in raw_bars), key=lambda row: row["trade_date"]
            )
            bar_days = {row["trade_date"] for row in normalized}
            if not normalized or len(bar_days) != len(normalized) or not bar_days.issubset(set(days)):
                raise QuantSystemError(f"invalid or empty bar history: {code}")
            for row in normalized:
                if (
                    not 0
                    < row["low"]
                    <= min(row["open"], row["close"])
                    <= max(row["open"], row["close"])
                    <= row["high"]
                    or row["volume"] < 0
                ):
                    raise QuantSystemError(f"invalid OHLCV: {code}/{row['trade_date']}")
            dividend_rows = (
                self._query("dividend", {"ts_code": code}, DIVIDEND_FIELDS)
                if dividend_source == "tushare"
                else [row for row in public_dividends if row["ts_code"] == code]
            )
            cash_by_day, dividend_audit = cash_dividends(code, dividend_rows, normalized, start, end)
            reconciliations.extend(dividend_audit)
            for ex_day, cash in sorted(cash_by_day.items()):
                actions.append(
                    {
                        "symbol": symbol,
                        "kind": "CASH_DIVIDEND",
                        "ex_date": ex_day.isoformat(),
                        "cash_per_share": str(cash),
                        "split_ratio": "1",
                        "available_at": cn_dt(ex_day, 9, 0).isoformat(),
                    }
                )
            if adjustment_source == "tushare":
                raw_factors = self._query("adj_factor", {"ts_code": code, **params}, ADJ_FACTOR_FIELDS)
                by_day = {
                    vendor_date(row["trade_date"]): Decimal(str(row["adj_factor"])) for row in raw_factors
                }
                if len(by_day) != len(raw_factors) or not bar_days.issubset(by_day):
                    raise QuantSystemError(f"missing or duplicated adjustment history: {code}")
            else:
                by_day = cash_adjustment_factors(normalized, cash_by_day)
            factors.extend(
                {
                    "symbol": symbol,
                    "trade_date": day,
                    "factor": float(by_day[day]),
                    "available_at": bar_available_at(day),
                }
                for day in sorted(bar_days)
            )
            restore_unadjusted_reference(code, normalized, cash_by_day, by_day)
            for day in sorted(set(days) - bar_days):
                statuses.append(
                    {
                        "symbol": symbol,
                        "flag": "SUSPENDED",
                        "effective_from": day.isoformat(),
                        "effective_to": day.isoformat(),
                        "available_at": cn_dt(day, 9, 0).isoformat(),
                    }
                )
            names = (
                self._query("namechange", {"ts_code": code}, NAMECHANGE_FIELDS)
                if reference_source == "tushare"
                else []
            )
            for row in names:
                if "ST" not in str(row["name"]).upper():
                    continue
                effective = vendor_date(row["start_date"])
                until = vendor_date(row["end_date"]) if row.get("end_date") else None
                if effective <= end and (until is None or until >= start):
                    statuses.append(
                        {
                            "symbol": symbol,
                            "flag": "ST",
                            "effective_from": effective.isoformat(),
                            "effective_to": until.isoformat() if until else None,
                            "available_at": cn_dt(effective, 9, 0).isoformat(),
                        }
                    )
            bars.extend(normalized)
            counts[symbol] = len(normalized)
        benchmarks: list[dict[str, Any]] = [
            {
                "symbol": benchmark,
                "trade_date": vendor_date(row["trade_date"]),
                "close": float(row["close"]),
                "prev_close": float(row["pre_close"]),
                "available_at": bar_available_at(vendor_date(row["trade_date"])),
            }
            for row in index_rows
        ]
        if any(row["close"] <= 0 or row["prev_close"] <= 0 for row in benchmarks):
            raise QuantSystemError("invalid benchmark prices")
        metadata = {
            "source": "Tushare Pro",
            "kind": "real_fixed_basket_baseline_v1",
            "protocol_hash": protocol_hash,
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "symbols": list(symbols),
            "benchmark": benchmark,
            "benchmark_source": benchmark_source,
            "reference_source": reference_source,
            "adjustment_source": adjustment_source,
            "dividend_source": dividend_source,
            "historical_st_certified": reference_source == "tushare",
            "bars_by_symbol": counts,
            "n_sessions": len(days),
            "n_cash_dividends": len(actions),
            "dividend_reconciliations": reconciliations,
            "calendar_source": "benchmark index_daily sessions",
            "raw_response_hashes": dict(sorted(self.raw_manifest.items())),
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".prepare-") as temporary:
            staged = Path(temporary) / "dataset"
            staged.mkdir()
            for name, rows in (
                ("bars", sorted(bars, key=lambda row: (row["trade_date"], row["symbol"]))),
                ("adjustments", factors),
                ("benchmarks", sorted(benchmarks, key=lambda row: row["trade_date"])),
            ):
                pq.write_table(pa.Table.from_pylist(rows), staged / f"{name}.parquet")
            for name, payload in (
                ("securities", securities),
                ("status_intervals", statuses),
                ("corporate_actions", actions),
                ("dataset_metadata", metadata),
            ):
                (staged / f"{name}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
                )
            for relative in self.raw_manifest:
                frozen_raw = staged / "raw" / relative
                frozen_raw.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.raw_root / relative, frozen_raw)
            staged.rename(destination)
        return metadata
