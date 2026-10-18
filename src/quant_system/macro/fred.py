"""FRED observations parsing; current downloads are not historical vintages."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from quant_system.macro.models import SERIES, MacroObservation

FRED_SERIES = frozenset({"DGS10", "DFII10", "DTWEXBGS", "DEXCHUS"})


def parse_fred_observations(
    payload: dict[str, Any],
    *,
    series_id: str,
    ingested_at: datetime,
    raw_content_hash: str = "",
) -> list[MacroObservation]:
    if series_id not in FRED_SERIES:
        raise ValueError("unsupported FRED series")
    rows = payload.get("observations")
    if not isinstance(rows, list):
        raise ValueError("FRED response has no observations array")
    observations: dict[date, MacroObservation] = {}
    for row in rows:
        if not isinstance(row, dict) or "value" not in row or "date" not in row:
            raise ValueError("malformed FRED observation")
        if row["value"] == ".":
            continue
        observed = date.fromisoformat(str(row["date"]))
        observation = MacroObservation(
            series_id=series_id,
            observation_date=observed,
            period=observed.isoformat(),
            value=float(row["value"]),
            unit=SERIES[series_id].unit,
            source="FRED",
            source_url=SERIES[series_id].source_url,
            ingested_at=ingested_at,
            available_at=ingested_at,
            vintage={
                "kind": "current_download_not_historical_pit",
                "realtime_start": str(row.get("realtime_start", payload.get("realtime_start", ""))),
                "realtime_end": str(row.get("realtime_end", payload.get("realtime_end", ""))),
            },
            raw_content_hash=raw_content_hash,
        )
        if observed in observations and observations[observed] != observation:
            raise ValueError("conflicting duplicate FRED observation")
        observations[observed] = observation
    return [observations[observed] for observed in sorted(observations)]
