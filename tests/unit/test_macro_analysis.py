from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from quant_system.macro.analysis import analyze_observations, series_coverage
from quant_system.macro.fred import parse_fred_observations
from quant_system.macro.models import SERIES

FETCHED_AT = datetime(2026, 10, 17, 8, tzinfo=UTC)


def rows(series_id: str, values: list[tuple[str, str]]):
    return parse_fred_observations(
        {"observations": [{"date": observed, "value": value} for observed, value in values]},
        series_id=series_id,
        ingested_at=FETCHED_AT,
        raw_content_hash="raw-hash",
    )


def analysis(observations, *, cutoff=None):
    return analyze_observations(
        observations,
        series_ids=["DGS10", "DFII10"],
        snapshot_id="macro-1",
        raw_snapshot_id="raw-1",
        content_hash="snapshot-hash",
        as_of=FETCHED_AT,
        available_at=FETCHED_AT,
        missing={},
        knowledge_cutoff=cutoff,
    )


def test_macro_analysis_yield_basis_points_common_date_spread_and_evidence() -> None:
    observations = rows("DGS10", [("2026-10-15", "4.00"), ("2026-10-16", "4.10")])
    observations += rows("DFII10", [("2026-10-15", "-0.50")])
    result = analysis(observations)
    assert result == analysis(observations)
    change = result["indicators"]["DGS10"]["change_from_previous_observation"]
    assert change["change_basis_points"] == pytest.approx(10)
    spread = result["indicators"]["US_10Y_NOMINAL_REAL_SPREAD"]
    assert spread["observation_date"] == "2026-10-15"
    assert spread["value"] == 4.5
    evidence = result["evidence_entries"][0]
    assert evidence["snapshot_id"] == "macro-1"
    assert evidence["artifact_id"] == "macro-1"
    assert datetime.fromisoformat(evidence["available_at"]) == FETCHED_AT
    assert evidence["raw_content_hash"] == "raw-hash"
    assert evidence["content_hash"] == "snapshot-hash"


def test_macro_current_download_cannot_be_made_historical_by_cutoff() -> None:
    observations = rows("DGS10", [("2023-01-03", "3.5")])
    historical = analysis(observations, cutoff=FETCHED_AT - timedelta(seconds=1))
    assert historical["indicators"] == {}
    assert historical["evidence_entries"] == []
    assert historical["observations"] == []
    assert historical["coverage"]["DGS10"]["count"] == 0
    assert historical["observations_metadata"]["excluded_unavailable"] == 1
    assert analysis(observations, cutoff=FETCHED_AT)["coverage"]["DGS10"]["count"] == 1
    assert analysis(observations, cutoff=FETCHED_AT + timedelta(days=1))["coverage"]["DGS10"]["count"] == 1


def test_macro_staleness_respects_release_frequency_and_semantic_labels() -> None:
    observations = rows("DGS10", [("2026-10-07", "1")]) + rows("DEXCHUS", [("2026-10-07", "7")])
    coverage = series_coverage(observations, series_ids=["DGS10", "DEXCHUS"], as_of=FETCHED_AT)
    assert coverage["DGS10"].stale
    assert not coverage["DEXCHUS"].stale
    assert coverage["DEXCHUS"].observation_frequency == "daily"
    assert coverage["DEXCHUS"].release_frequency == "weekly"
    assert "not ICE DXY" in SERIES["DTWEXBGS"].limitation
    assert "not PBOC" in SERIES["DEXCHUS"].limitation


def test_macro_analysis_rejects_naive_cutoff() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        analysis([], cutoff=datetime(2026, 1, 1))
