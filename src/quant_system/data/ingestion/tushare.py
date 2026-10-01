"""Tushare Pro ingestion into the local PIT dataset contract."""

from __future__ import annotations

import json
import os
import re
import ssl
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi
import pyarrow as pa
import pyarrow.parquet as pq

from quant_system.domain.exceptions import QuantSystemError
from quant_system.domain.types import bar_available_at, cn_dt

TUSHARE_ENDPOINT = "https://api.tushare.pro"
DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,vol,amount"
SECURITY_FIELDS = "ts_code,name,market,exchange,list_status,list_date,delist_date"
ADJ_FACTOR_FIELDS = "ts_code,trade_date,adj_factor"
SUSPEND_FIELDS = "ts_code,trade_date,suspend_timing,suspend_type"
NAMECHANGE_FIELDS = "ts_code,name,start_date,end_date,ann_date,change_reason"
INDEX_DAILY_FIELDS = "ts_code,trade_date,close,pre_close,change,pct_chg,vol,amount"
FUTURES_DAILY_FIELDS = (
    "ts_code,trade_date,pre_close,pre_settle,open,high,low,close,settle,vol,amount,oi"
)
FUTURES_EXCHANGES = ("CFFEX", "SHFE", "INE", "DCE", "CZCE", "GFEX")
BENCHMARK_INDICES = (
    "000001.SH",
    "399001.SZ",
    "399006.SZ",
    "000300.SH",
    "000905.SH",
    "000852.SH",
)

Transport = Callable[[str, dict[str, Any], float], dict[str, Any]]


def _http_transport(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise QuantSystemError(f"Tushare returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise QuantSystemError(f"Tushare connection failed: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise QuantSystemError("Tushare response must be a JSON object")
    return payload


class TushareClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        endpoint: str = TUSHARE_ENDPOINT,
        timeout_seconds: float = 60,
        transport: Transport | None = None,
    ) -> None:
        self._token = (token or os.environ.get("TUSHARE_TOKEN", "")).strip()
        if not self._token:
            raise QuantSystemError("missing Tushare credential: set TUSHARE_TOKEN")
        if not endpoint.startswith("https://"):
            raise QuantSystemError("Tushare endpoint must use HTTPS")
        self._endpoint = endpoint
        self._timeout = timeout_seconds
        self._transport = transport or _http_transport

    def query(
        self,
        api_name: str,
        *,
        params: dict[str, Any],
        fields: str,
    ) -> list[dict[str, Any]]:
        payload = self._transport(
            self._endpoint,
            {"api_name": api_name, "token": self._token, "params": params, "fields": fields},
            self._timeout,
        )
        code = payload.get("code")
        if code != 0:
            message = str(payload.get("msg") or "unknown error")
            raise QuantSystemError(f"Tushare {api_name} failed (code={code}): {message}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise QuantSystemError(f"Tushare {api_name} response has no data object")
        names = data.get("fields")
        items = data.get("items")
        if not isinstance(names, list) or not isinstance(items, list):
            raise QuantSystemError(f"Tushare {api_name} response has invalid fields/items")
        return [dict(zip(names, item, strict=True)) for item in items]


@dataclass(frozen=True)
class TushareSyncResult:
    output_dir: str
    start_date: str
    end_date: str
    trading_days: int
    fetched_bars: int
    total_bars: int
    securities: int


class TushareDailySync:
    """Synchronize unadjusted SSE/SZSE daily bars into the local file contract."""

    def __init__(self, client: TushareClient, *, request_interval: float = 0.13) -> None:
        if request_interval < 0:
            raise ValueError("request_interval must be non-negative")
        self._client = client
        self._request_interval = request_interval

    def sync(
        self,
        output_dir: str | Path,
        *,
        start: date,
        end: date,
        overwrite: bool = False,
    ) -> TushareSyncResult:
        if start > end:
            raise ValueError("start date must be on or before end date")
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)

        securities = self._fetch_securities()
        trading_days = self._fetch_trading_days(start, end)
        fetched: list[dict[str, Any]] = []
        for index, trading_day in enumerate(trading_days):
            if index and self._request_interval:
                time.sleep(self._request_interval)
            rows = self._client.query(
                "daily",
                params={"trade_date": trading_day.strftime("%Y%m%d")},
                fields=DAILY_FIELDS,
            )
            fetched.extend(self._normalize_bar(row) for row in rows if _is_supported_code(row))

        bars_path = destination / "bars.parquet"
        existing = [] if overwrite else _read_existing_bars(bars_path)
        merged = _merge_bars(existing, fetched)
        _write_parquet_atomic(bars_path, merged)
        _write_json_atomic(destination / "securities.json", securities)
        _write_json_atomic(destination / "status_intervals.json", [])
        _write_json_atomic(destination / "corporate_actions.json", [])

        metadata = {
            "source": "Tushare Pro",
            "dataset": "unadjusted_daily_sse_szse_v0",
            "synced_at": datetime.now(UTC).isoformat(),
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "units": {"volume": "shares", "amount": "CNY"},
            "pit": {
                "bars_available_at": "trade_date 17:00 Asia/Shanghai",
                "security_master": "versioned at listing/delisting dates",
                "status_intervals": "not synchronized in V0",
                "corporate_actions": "not synchronized in V0",
            },
        }
        _write_json_atomic(destination / "dataset_metadata.json", metadata)
        return TushareSyncResult(
            output_dir=str(destination),
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            trading_days=len(trading_days),
            fetched_bars=len(fetched),
            total_bars=len(merged),
            securities=len({row["symbol"] for row in securities}),
        )

    def _fetch_trading_days(self, start: date, end: date) -> list[date]:
        rows = self._client.query(
            "trade_cal",
            params={
                "exchange": "SSE",
                "start_date": start.strftime("%Y%m%d"),
                "end_date": end.strftime("%Y%m%d"),
                "is_open": "1",
            },
            fields="cal_date,is_open",
        )
        return sorted(_parse_date(row["cal_date"]) for row in rows if str(row["is_open"]) == "1")

    def _fetch_securities(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for status in ("L", "D", "P"):
            rows.extend(
                self._client.query(
                    "stock_basic",
                    params={"exchange": "", "list_status": status},
                    fields=SECURITY_FIELDS,
                )
            )
        records: list[dict[str, Any]] = []
        for row in rows:
            if not _is_supported_code(row):
                continue
            listed = _parse_date(row["list_date"])
            base = {
                "symbol": _normalize_symbol(str(row["ts_code"])),
                "name": str(row["name"]),
                "exchange": _normalize_exchange(str(row["exchange"])),
                "board": _normalize_board(str(row.get("market") or "")),
                "security_type": "STOCK",
                "list_date": listed.isoformat(),
                "delist_date": None,
                "available_at": cn_dt(listed, 9, 0).isoformat(),
            }
            records.append(base)
            raw_delist = str(row.get("delist_date") or "").strip()
            if raw_delist:
                delisted = _parse_date(raw_delist)
                records.append(
                    {
                        **base,
                        "delist_date": delisted.isoformat(),
                        "available_at": cn_dt(delisted, 9, 0).isoformat(),
                    }
                )
        unique = {(row["symbol"], row["available_at"]): row for row in records}
        return sorted(unique.values(), key=lambda row: (row["symbol"], row["available_at"]))

    @staticmethod
    def _normalize_bar(row: dict[str, Any]) -> dict[str, Any]:
        trading_day = _parse_date(row["trade_date"])
        volume = int((Decimal(str(row["vol"])) * Decimal("100")).to_integral_value())
        amount = Decimal(str(row["amount"])) * Decimal("1000")
        return {
            "symbol": _normalize_symbol(str(row["ts_code"])),
            "trade_date": trading_day,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "prev_close": float(row["pre_close"]),
            "volume": volume,
            "amount": float(amount),
            "available_at": bar_available_at(trading_day),
        }


@dataclass(frozen=True)
class TushareMarketDayResult:
    output_dir: str
    trade_date: str
    datasets: dict[str, list[dict[str, Any]]]
    unavailable_optional_datasets: tuple[str, ...]
    acquisition_errors: tuple[str, ...] = ()


class TushareMarketDaySync:
    """Fetch and normalize one complete A-share and domestic-futures EOD session."""

    def __init__(self, client: TushareClient, *, request_interval: float = 0.13) -> None:
        if request_interval < 0:
            raise ValueError("request_interval must be non-negative")
        self._client = client
        self._request_interval = request_interval
        self._calls = 0
        self._known_open_dates: set[date] = set()

    def latest_open_date(self, end: date, *, lookback_days: int = 14) -> date | None:
        start = date.fromordinal(max(1, end.toordinal() - lookback_days))
        rows = self._query(
            "trade_cal",
            params={
                "exchange": "SSE",
                "start_date": start.strftime("%Y%m%d"),
                "end_date": end.strftime("%Y%m%d"),
                "is_open": "1",
            },
            fields="cal_date,is_open",
        )
        open_dates = [_parse_date(row["cal_date"]) for row in rows if str(row["is_open"]) == "1"]
        self._known_open_dates.update(open_dates)
        return max(open_dates) if open_dates else None

    def sync(
        self,
        output_dir: str | Path,
        *,
        trade_date: date,
        include_futures: bool = True,
        verify_trade_date: bool = True,
    ) -> TushareMarketDayResult:
        day = trade_date.strftime("%Y%m%d")
        acquisition_errors: list[str] = []
        if not verify_trade_date:
            calendar: list[dict[str, Any]] = []
        elif trade_date in self._known_open_dates:
            calendar = [{"cal_date": day, "is_open": 1}]
        else:
            try:
                calendar = self._query(
                    "trade_cal",
                    params={"exchange": "SSE", "start_date": day, "end_date": day},
                    fields="cal_date,is_open",
                )
            except QuantSystemError as exc:
                calendar = []
                acquisition_errors.append(f"trade_calendar: {exc}")
        if calendar and not any(str(row.get("is_open")) == "1" for row in calendar):
            raise QuantSystemError(f"{trade_date.isoformat()} is not an SSE trading day")

        try:
            equity_rows = self._query("daily", params={"trade_date": day}, fields=DAILY_FIELDS)
        except QuantSystemError as exc:
            equity_rows = []
            acquisition_errors.append(f"equity_daily: {exc}")
        futures_raw: list[dict[str, Any]] = []
        if include_futures:
            for exchange in FUTURES_EXCHANGES:
                try:
                    futures_raw.extend(
                        self._query(
                            "fut_daily",
                            params={"trade_date": day, "exchange": exchange},
                            fields=FUTURES_DAILY_FIELDS,
                        )
                    )
                except QuantSystemError as exc:
                    acquisition_errors.append(f"futures_daily[{exchange}]: {exc}")
                    break

        datasets: dict[str, list[dict[str, Any]]] = {
            "trade_calendar": (
                [{"trade_date": trade_date, "is_open": 1, "exchange": "SSE"}]
                if calendar
                else []
            ),
            "equity_daily": [
                TushareDailySync._normalize_bar(row)
                for row in equity_rows
                if _is_supported_code(row)
            ],
            "futures_daily": [self._normalize_futures_bar(row) for row in futures_raw],
        }
        optional_queries = {
            "adj_factor": ("adj_factor", {"trade_date": day}, ADJ_FACTOR_FIELDS),
            "suspensions": ("suspend_d", {"trade_date": day}, SUSPEND_FIELDS),
            "name_changes": (
                "namechange",
                {"start_date": day, "end_date": day},
                NAMECHANGE_FIELDS,
            ),
        }
        unavailable: list[str] = []
        for dataset, (api_name, params, fields) in optional_queries.items():
            try:
                datasets[dataset] = self._query(api_name, params=params, fields=fields)
            except QuantSystemError as exc:
                datasets[dataset] = []
                unavailable.append(dataset)
                acquisition_errors.append(f"{dataset}: {exc}")

        index_rows: list[dict[str, Any]] = []
        try:
            for index_code in BENCHMARK_INDICES:
                index_rows.extend(
                    self._query(
                        "index_daily",
                        params={"ts_code": index_code, "trade_date": day},
                        fields=INDEX_DAILY_FIELDS,
                    )
                )
        except QuantSystemError as exc:
            index_rows = []
            unavailable.append("index_daily")
            acquisition_errors.append(f"index_daily: {exc}")
        datasets["index_daily"] = index_rows

        destination = Path(output_dir) / trade_date.isoformat()
        destination.mkdir(parents=True, exist_ok=True)
        for name, rows in datasets.items():
            path = destination / f"{name}.parquet"
            if rows:
                _write_parquet_atomic(path, rows)
            elif path.exists():
                path.unlink()
        _write_json_atomic(
            destination / "dataset_metadata.json",
            {
                "source": "Tushare Pro",
                "trade_date": trade_date.isoformat(),
                "available_at": bar_available_at(trade_date).isoformat(),
                "datasets": {name: len(rows) for name, rows in sorted(datasets.items())},
                "unavailable_optional_datasets": sorted(set(unavailable)),
                "acquisition_errors": acquisition_errors,
            },
        )
        return TushareMarketDayResult(
            output_dir=str(destination),
            trade_date=trade_date.isoformat(),
            datasets=datasets,
            unavailable_optional_datasets=tuple(sorted(set(unavailable))),
            acquisition_errors=tuple(acquisition_errors),
        )

    def _query(
        self,
        api_name: str,
        *,
        params: dict[str, Any],
        fields: str,
    ) -> list[dict[str, Any]]:
        if self._calls and self._request_interval:
            time.sleep(self._request_interval)
        self._calls += 1
        return self._client.query(api_name, params=params, fields=fields)

    @staticmethod
    def _normalize_futures_bar(row: dict[str, Any]) -> dict[str, Any]:
        trading_day = _parse_date(row["trade_date"])
        symbol = _normalize_futures_symbol(str(row["ts_code"]))
        return {
            "symbol": symbol,
            "product": _futures_product(symbol),
            "trading_day": trading_day,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "settlement": float(row["settle"]) if row.get("settle") is not None else None,
            "pre_settlement": (
                float(row["pre_settle"]) if row.get("pre_settle") is not None else None
            ),
            "volume": int(row.get("vol") or 0),
            "amount": float(row.get("amount") or 0),
            "open_interest": int(row.get("oi") or 0),
            "available_at": bar_available_at(trading_day),
        }


def _parse_date(value: object) -> date:
    return datetime.strptime(str(value), "%Y%m%d").date()


def _is_supported_code(row: dict[str, Any]) -> bool:
    return str(row.get("ts_code", "")).endswith((".SH", ".SZ"))


def _normalize_symbol(ts_code: str) -> str:
    if ts_code.endswith(".SH"):
        return f"{ts_code[:-3]}.SSE"
    if ts_code.endswith(".SZ"):
        return f"{ts_code[:-3]}.SZSE"
    raise QuantSystemError(f"unsupported Tushare stock code: {ts_code}")


def _normalize_futures_symbol(ts_code: str) -> str:
    code, separator, suffix = ts_code.rpartition(".")
    mapping = {
        "SHF": "SHFE",
        "SHFE": "SHFE",
        "DCE": "DCE",
        "ZCE": "CZCE",
        "CZC": "CZCE",
        "CZCE": "CZCE",
        "CFX": "CFFEX",
        "CFFEX": "CFFEX",
        "INE": "INE",
        "GFE": "GFEX",
        "GFEX": "GFEX",
    }
    if not separator or suffix not in mapping:
        raise QuantSystemError(f"unsupported Tushare futures code: {ts_code}")
    return f"{code}.{mapping[suffix]}"


def _futures_product(symbol: str) -> str:
    match = re.match(r"[A-Za-z]+", symbol)
    if match is None:
        raise QuantSystemError(f"cannot derive futures product from {symbol}")
    return match.group(0).lower()


def _normalize_exchange(exchange: str) -> str:
    mapping = {"SSE": "SSE", "SZSE": "SZSE"}
    try:
        return mapping[exchange]
    except KeyError as exc:
        raise QuantSystemError(f"unsupported Tushare exchange: {exchange}") from exc


def _normalize_board(market: str) -> str:
    mapping = {"科创板": "STAR", "创业板": "CHINEXT"}
    return mapping.get(market, "MAIN")


def _read_existing_bars(path: Path) -> list[dict[str, Any]]:
    return pq.read_table(path).to_pylist() if path.exists() else []


def _merge_bars(
    existing: Iterable[dict[str, Any]], fetched: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = {(row["symbol"], row["trade_date"]): row for row in existing}
    rows.update({(row["symbol"], row["trade_date"]): row for row in fetched})
    return sorted(rows.values(), key=lambda row: (row["trade_date"], row["symbol"]))


def _write_parquet_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise QuantSystemError("Tushare sync returned no daily bars")
    temporary = path.with_suffix(".parquet.tmp")
    pq.write_table(pa.Table.from_pylist(rows), temporary)
    temporary.replace(path)


def _write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    temporary.replace(path)


def result_as_json(result: TushareSyncResult) -> str:
    return json.dumps(asdict(result), ensure_ascii=False, indent=2)