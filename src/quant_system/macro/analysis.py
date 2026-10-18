"""Deterministic macro facts; no model calls, recommendations or network I/O."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from quant_system.domain.types import require_aware
from quant_system.macro.models import SERIES, MacroObservation, SeriesCoverage

LIMITATIONS = [
    "Current downloads of historical observations are not certified historical point-in-time data.",
    "Without original release/vintage evidence, available_at is the actual ingestion time, not the observation date.",
    "No forward filling, original-vintage reconstruction, hidden source fallback or historical coverage guarantee.",
    "Series have different observation and release frequencies; observation dates are not publication timestamps.",
    "Reserve quantity changes are changes in China's reported stock, not global central bank demand.",
    "This deterministic descriptive analysis is not a trading signal, causal attribution or investment recommendation.",
]


def series_coverage(
    observations: Sequence[MacroObservation],
    *,
    series_ids: list[str],
    as_of: datetime,
) -> dict[str, SeriesCoverage]:
    require_aware(as_of)
    result = {}
    for series_id in series_ids:
        rows = sorted(
            (row for row in observations if row.series_id == series_id), key=lambda row: row.observation_date
        )
        definition = SERIES[series_id]
        last = rows[-1].observation_date if rows else None
        stale = last is None or (as_of.date() - last).days > definition.stale_after_days
        result[series_id] = SeriesCoverage(
            count=len(rows),
            first_observation=rows[0].observation_date if rows else None,
            last_observation=last,
            observation_frequency=definition.observation_frequency,
            release_frequency=definition.release_frequency,
            stale=stale,
            status="MISSING" if not rows else "STALE" if stale else "AVAILABLE",
        )
    return result


def _evidence_id(snapshot_id: str, observation: MacroObservation) -> str:
    return f"macro:{snapshot_id}:{observation.series_id}:{observation.observation_date.isoformat()}"


def _change(latest: MacroObservation, previous: MacroObservation) -> dict[str, Any]:
    delta = latest.value - previous.value
    change: dict[str, Any] = {
        "from_period": previous.period,
        "to_period": latest.period,
        "from_date": previous.observation_date.isoformat(),
        "to_date": latest.observation_date.isoformat(),
        "absolute_change": delta,
        "unit": latest.unit,
    }
    if latest.unit == "percent":
        change["change_percentage_points"] = delta
        change["change_basis_points"] = delta * 100
    else:
        change["change_percent"] = (latest.value / previous.value - 1) * 100 if previous.value != 0 else None
    return change


def analyze_observations(
    observations: Sequence[MacroObservation],
    *,
    series_ids: list[str],
    snapshot_id: str,
    raw_snapshot_id: str,
    content_hash: str,
    as_of: datetime,
    available_at: datetime,
    missing: dict[str, str],
    knowledge_cutoff: datetime | None = None,
) -> dict[str, Any]:
    cutoff = require_aware(knowledge_cutoff or as_of)
    visible = sorted(
        (row for row in observations if row.available_at <= cutoff),
        key=lambda row: (row.series_id, row.observation_date),
    )
    visible_missing = dict(missing)
    coverage = series_coverage(visible, series_ids=series_ids, as_of=cutoff)
    indicators: dict[str, Any] = {}
    evidence = []
    for row in visible:
        evidence.append(
            {
                "evidence_id": _evidence_id(snapshot_id, row),
                "namespace": "macro",
                "kind": "fact",
                "snapshot_id": snapshot_id,
                "artifact_id": snapshot_id,
                "content_hash": content_hash,
                "schema_version": "macro-snapshot-v1",
                "raw_snapshot_id": raw_snapshot_id,
                "raw_content_hash": row.raw_content_hash,
                "record_key": f"observations/{row.series_id}/{row.observation_date.isoformat()}",
                **row.model_dump(mode="json"),
            }
        )
    for series_id in series_ids:
        rows = [row for row in visible if row.series_id == series_id]
        if not rows:
            visible_missing.setdefault(series_id, "NO_OBSERVATIONS_AVAILABLE_AT_KNOWLEDGE_CUTOFF")
            continue
        latest = rows[-1]
        indicator: dict[str, Any] = {
            "title": SERIES[series_id].title,
            "latest_value": latest.value,
            "unit": latest.unit,
            "observation_date": latest.observation_date.isoformat(),
            "period": latest.period,
            "available_at": latest.available_at.isoformat(),
            "source_url": latest.source_url,
            "evidence_ids": [_evidence_id(snapshot_id, latest)],
            "change_from_previous_observation": None,
            "change_over_20_observations": None,
            "limitation": SERIES[series_id].limitation,
        }
        if len(rows) >= 2:
            indicator["change_from_previous_observation"] = {
                **_change(latest, rows[-2]),
                "evidence_ids": [_evidence_id(snapshot_id, rows[-2]), _evidence_id(snapshot_id, latest)],
            }
        if len(rows) >= 21 and SERIES[series_id].observation_frequency == "daily":
            indicator["change_over_20_observations"] = {
                **_change(latest, rows[-21]),
                "observations_back": 20,
                "evidence_ids": [_evidence_id(snapshot_id, rows[-21]), _evidence_id(snapshot_id, latest)],
            }
        indicators[series_id] = indicator
    nominal = {row.observation_date: row for row in visible if row.series_id == "DGS10"}
    real = {row.observation_date: row for row in visible if row.series_id == "DFII10"}
    common = nominal.keys() & real.keys()
    if common:
        observed = max(common)
        nominal_row, real_row = nominal[observed], real[observed]
        indicators["US_10Y_NOMINAL_REAL_SPREAD"] = {
            "value": nominal_row.value - real_row.value,
            "unit": "percentage_points",
            "observation_date": observed.isoformat(),
            "available_at": max(nominal_row.available_at, real_row.available_at).isoformat(),
            "method": "DGS10 minus DFII10 on the latest common observation date, no forward fill",
            "limitation": "A yield spread, not a pure inflation forecast; liquidity and risk premia are not removed.",
            "evidence_ids": [_evidence_id(snapshot_id, nominal_row), _evidence_id(snapshot_id, real_row)],
        }
    limitations = list(LIMITATIONS)
    excluded = len(observations) - len(visible)
    if excluded:
        limitations.append(f"{excluded} observations excluded because available_at exceeds knowledge_cutoff.")
    if visible_missing:
        limitations.append("Coverage is partial; the missing entries are not zero-valued observations.")
    return {
        "schema_version": "macro-analysis-v1",
        "snapshot_id": snapshot_id,
        "raw_snapshot_id": raw_snapshot_id,
        "content_hash": content_hash,
        "as_of": as_of.isoformat(),
        "available_at": available_at.isoformat(),
        "knowledge_cutoff": cutoff.isoformat(),
        "historical_pit_certified": False,
        "indicators": indicators,
        "coverage": {key: value.model_dump(mode="json") for key, value in coverage.items()},
        "missing": visible_missing,
        "limitations": limitations,
        "evidence_entries": evidence,
        "observations": [row.model_dump(mode="json") for row in visible],
        "observations_metadata": {
            "total_in_snapshot": len(observations),
            "visible": len(visible),
            "excluded_unavailable": excluded,
            "original_publication_times_known": sum(row.published_at is not None for row in visible),
            "availability_policy": "actual_fetch_time_without_original_vintage_evidence",
        },
    }
