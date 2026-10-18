from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.gold.models import PricePoint, Quote
from quant_system.gold.service import GoldService, completed_eod_date
from quant_system.infra.db import create_all, make_engine, make_session_factory


def _service(tmp_path: Path) -> GoldService:
    engine = make_engine(f"sqlite:///{tmp_path / 'gold.db'}")
    create_all(engine)
    return GoldService(LocalArtifactStore(tmp_path / "artifacts"), ArtifactRegistry(make_session_factory(engine)))


def _quote(symbol: str, as_of: date, fetched_at: datetime) -> Quote:
    return Quote(
        symbol=symbol, name=symbol, kind="spot" if symbol == "Au99.99" else "etf",
        unit="CNY/g" if symbol == "Au99.99" else "CNY/share",
        source="test", source_url="https://example.invalid", fetched_at=fetched_at,
        points=[PricePoint(date=as_of, close="10")],
    )


def test_refresh_always_fetches_and_freezes_a_new_snapshot(tmp_path: Path) -> None:
    service = _service(tmp_path)
    calls = []

    def fetcher(as_of, now):
        calls.append(as_of)
        return [_quote("Au99.99", as_of, now), _quote("518880", as_of, now)], {"fetch": now.isoformat()}, []

    now = datetime(2026, 10, 16, 2, tzinfo=UTC)
    first, _ = service.refresh(fetched_at=now, fetcher=fetcher)
    second, snapshot = service.refresh(fetched_at=now + timedelta(minutes=1), fetcher=fetcher)
    assert calls == [date(2026, 10, 15), date(2026, 10, 15)]
    assert first.artifact_id != second.artifact_id
    assert first.artifact_id in second.parent_artifact_ids
    assert second.data_snapshot_id in second.parent_artifact_ids
    assert service.load_snapshot(second.artifact_id) == snapshot


def test_refresh_retains_missing_quote_without_claiming_it_was_refetched(tmp_path: Path) -> None:
    service = _service(tmp_path)
    now = datetime(2026, 10, 16, 2, tzinfo=UTC)

    def complete(as_of, fetched_at):
        return [_quote("Au99.99", as_of, fetched_at), _quote("518880", as_of, fetched_at)], {}, []

    def partial(as_of, fetched_at):
        return [_quote("Au99.99", as_of, fetched_at)], {}, ["518880 unavailable"]

    service.refresh(fetched_at=now, fetcher=complete)
    _, snapshot = service.refresh(fetched_at=now + timedelta(hours=1), fetcher=partial)
    assert snapshot.quotes[1].fetched_at == now
    assert snapshot.quotes[0].fetched_at == snapshot.fetched_at
    assert any("retained" in warning for warning in snapshot.warnings)


def test_refresh_rejects_unfinished_or_future_observations(tmp_path: Path) -> None:
    service = _service(tmp_path)
    now = datetime(2026, 10, 16, 2, tzinfo=UTC)
    assert completed_eod_date(now) == date(2026, 10, 15)
    assert completed_eod_date(now + timedelta(hours=8)) == date(2026, 10, 16)

    def future(as_of, fetched_at):
        return [_quote("Au99.99", as_of + timedelta(days=1), fetched_at)], {}, []

    with pytest.raises(ValueError, match="EOD cutoff"):
        service.refresh(as_of=date(2026, 10, 16), fetched_at=now, fetcher=future)
    with pytest.raises(ValueError, match="outside this fetch"):
        service.refresh(fetched_at=now, fetcher=future)