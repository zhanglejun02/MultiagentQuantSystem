from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime, timedelta

from scripts import sync_macro

from quant_system.macro.models import MacroCollection, MacroQualityError


def test_macro_cli_canary_bounds_window_and_analysis_does_not_collect_twice(
    tmp_path, monkeypatch, capsys
) -> None:
    captured = {}

    class FakeService:
        def __init__(self, store, registry, *, config):
            captured["config"] = config

        def collect(self, request):
            assert "request" not in captured
            captured["request"] = request
            now = datetime.now(UTC)
            return MacroCollection(
                snapshot_id="macro-test",
                raw_snapshot_id="raw-test",
                as_of=now,
                available_at=now,
                coverage={},
                missing={},
                summary={"status": "AVAILABLE"},
            )

        def analyze(self, snapshot_id):
            assert snapshot_id == "macro-test"
            return {"snapshot_id": snapshot_id, "indicators": {}}

    monkeypatch.setattr(sync_macro, "ROOT", tmp_path)
    monkeypatch.setattr(sync_macro, "MacroService", FakeService)
    config_path = sync_macro.__file__.replace("scripts/sync_macro.py", "configs/data/macro.yaml")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sync_macro.py",
            "--canary",
            "--analyze",
            "--series",
            "CN_GOLD_RESERVES",
            "--config",
            config_path,
            "--db-url",
            f"sqlite:///{tmp_path / 'macro.db'}",
            "--artifacts-root",
            str(tmp_path / "artifacts"),
        ],
    )
    assert sync_macro.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["collection"]["snapshot_id"] == "macro-test"
    assert output["analysis"]["snapshot_id"] == "macro-test"
    today = datetime.now(UTC).date()
    assert captured["request"].start_date >= max(today - timedelta(days=45), date(today.year, 1, 1))
    assert captured["request"].end_date == today
    assert captured["config"].fred_max_pages == 1
    assert captured["config"].safe_max_years == 1
    assert captured["config"].max_requests <= 8


def test_macro_cli_zero_data_outputs_frozen_raw_id_and_nonzero_exit(tmp_path, monkeypatch, capsys) -> None:
    class FakeService:
        def __init__(self, store, registry, *, config):
            pass

        def collect(self, request):
            raise MacroQualityError(["DGS10: FRED_API_KEY_NOT_CONFIGURED"], raw_snapshot_id="raw-failure")

    monkeypatch.setattr(sync_macro, "ROOT", tmp_path)
    monkeypatch.setattr(sync_macro, "MacroService", FakeService)
    config_path = sync_macro.__file__.replace("scripts/sync_macro.py", "configs/data/macro.yaml")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sync_macro.py",
            "--series",
            "DGS10",
            "--config",
            config_path,
            "--db-url",
            f"sqlite:///{tmp_path / 'macro.db'}",
            "--artifacts-root",
            str(tmp_path / "artifacts"),
        ],
    )
    assert sync_macro.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "FAILED",
        "quality_errors": ["DGS10: FRED_API_KEY_NOT_CONFIGURED"],
        "raw_snapshot_id": "raw-failure",
    }
