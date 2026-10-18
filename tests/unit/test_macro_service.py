from __future__ import annotations

import ast
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_macro_ingestion import OfflineTransport, fred_page
from tests.unit.test_macro_parsers import SAFE_TABLE, SAFE_URL

from quant_system.artifacts.envelope import ArtifactEnvelope, hash_bytes
from quant_system.domain.enums import ArtifactStatus, ArtifactType
from quant_system.macro import MacroAnalyzeRequest, MacroCollectRequest, MacroService
from quant_system.macro.ingestion import FRED_OBSERVATIONS_URL, MacroSourceConfig
from quant_system.macro.models import MacroQualityError

FETCHED_AT = datetime(2026, 10, 17, 8, tzinfo=UTC)


@pytest.fixture
def macro_type() -> ArtifactType:
    try:
        return ArtifactType("MACRO_SNAPSHOT")
    except ValueError:
        pytest.skip("shared ArtifactType.MACRO_SNAPSHOT is an external integration prerequisite")


def fred_transport(value: str = "4.1") -> OfflineTransport:
    return OfflineTransport(
        {
            f"{FRED_OBSERVATIONS_URL}:0": fred_page(0, 2, [("2026-10-15", "4"), ("2026-10-16", value)]),
        }
    )


def request() -> MacroCollectRequest:
    return MacroCollectRequest(
        series_ids=["DGS10"], start_date=date(2026, 10, 1), end_date=date(2026, 10, 17)
    )


def test_macro_has_no_gold_factor_agent_runtime_imports() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "quant_system" / "macro"
    forbidden = (
        "quant_system.gold",
        "quant_system.factors",
        "quant_system.agents",
        "quant_system.research.runtime",
    )
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else ([alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            )
            assert not any(name.startswith(forbidden) for name in names), path
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import quant_system.macro; "
            "assert not any(name.startswith(('quant_system.gold', 'quant_system.factors', 'quant_system.agents')) "
            "for name in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_macro_public_request_interface() -> None:
    assert MacroAnalyzeRequest(snapshot_id="snapshot").snapshot_id == "snapshot"


def test_macro_missing_enum_fails_before_network_or_writes(store, registry, monkeypatch) -> None:
    if "MACRO_SNAPSHOT" in ArtifactType.__members__:
        return
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    transport = fred_transport()
    with pytest.raises(RuntimeError, match="ArtifactType.MACRO_SNAPSHOT"):
        MacroService(store, registry, clock=lambda: FETCHED_AT, transport=transport).collect(request())
    assert transport.calls == []
    assert registry.list_all() == []


def test_macro_collect_parent_hash_metadata_and_offline_analysis(
    store, registry, monkeypatch, macro_type
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    transport = fred_transport()
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=transport)
    result = service.collect(request())
    snapshot = registry.get(result.snapshot_id)
    raw = registry.get(result.raw_snapshot_id)
    assert snapshot.artifact_type == macro_type
    assert raw.artifact_type == ArtifactType.DATA_SNAPSHOT
    assert snapshot.parent_artifact_ids == (raw.artifact_id,)
    assert snapshot.data_snapshot_id == raw.artifact_id
    assert hash_bytes(store.get_bytes(raw.uri)) == raw.content_hash
    assert hash_bytes(store.get_bytes(snapshot.uri)) == snapshot.content_hash
    assert result.coverage["DGS10"].count == 2
    assert result.available_at == FETCHED_AT
    assert result.summary["status"] == "AVAILABLE"
    assert "offline-secret" not in store.get_bytes(raw.uri).decode()
    assert "api_key" not in store.get_bytes(raw.uri).decode()
    transport.pages.clear()
    before = len(transport.calls)
    analyzed = service.analyze(result.snapshot_id)
    assert analyzed == service.analyze(result.snapshot_id)
    assert len(transport.calls) == before
    assert analyzed["indicators"]["DGS10"]["latest_value"] == 4.1
    assert all(entry["snapshot_id"] == result.snapshot_id for entry in analyzed["evidence_entries"])
    assert registry.list_all() and len(registry.list_all()) == 2


def test_macro_collect_safe_partial_without_key(store, registry, monkeypatch, macro_type) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    transport = OfflineTransport(
        {
            "https://www.safe.gov.cn/safe/whcb/index.html": f'<a href="{SAFE_URL}">Official reserve assets (2026)</a>'.encode(),
            SAFE_URL: SAFE_TABLE.encode(),
        }
    )
    result = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=transport).collect(
        MacroCollectRequest(start_date=date(2026, 1, 1)),
    )
    assert result.summary["status"] == "PARTIAL"
    assert result.missing["DGS10"] == "FRED_API_KEY_NOT_CONFIGURED"
    assert result.coverage["CN_GOLD_RESERVES"].count == 2
    assert result.coverage["CN_GOLD_RESERVES_USD"].count == 2


def test_macro_zero_data_fails_with_frozen_quality_errors(store, registry, monkeypatch, macro_type) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=OfflineTransport({}))
    with pytest.raises(MacroQualityError) as raised:
        service.collect(MacroCollectRequest())
    assert raised.value.quality_errors
    raw = registry.get(raised.value.raw_snapshot_id)
    assert raw.artifact_type == ArtifactType.DATA_SNAPSHOT
    assert raw.status == ArtifactStatus.FAILED
    assert store.get_json(raw.uri)["quality_errors"] == raised.value.quality_errors
    assert registry.find_by_type(macro_type) == []


def test_macro_revisions_append_and_preserve_first_snapshot(store, registry, monkeypatch, macro_type) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    current = [FETCHED_AT]
    transport = fred_transport("4.1")
    service = MacroService(store, registry, clock=lambda: current[0], transport=transport)
    first = service.collect(request())
    first_raw = registry.get(first.raw_snapshot_id)
    first_body = store.get_bytes(first_raw.uri)
    current[0] += timedelta(minutes=1)
    transport.pages = fred_transport("4.2").pages
    revised = service.collect(request())
    assert first.snapshot_id != revised.snapshot_id
    assert first.raw_snapshot_id != revised.raw_snapshot_id
    assert registry.get(revised.raw_snapshot_id).content_hash != first_raw.content_hash
    assert store.get_bytes(first_raw.uri) == first_body
    assert service.analyze(first.snapshot_id)["indicators"]["DGS10"]["latest_value"] == 4.1
    assert service.analyze(revised.snapshot_id)["indicators"]["DGS10"]["latest_value"] == 4.2


def test_macro_identical_raw_content_is_content_addressed_not_overwritten(
    store, registry, monkeypatch, macro_type
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=fred_transport())
    first, second = service.collect(request()), service.collect(request())
    first_raw, second_raw = registry.get(first.raw_snapshot_id), registry.get(second.raw_snapshot_id)
    assert first_raw.artifact_id != second_raw.artifact_id
    assert first_raw.uri == second_raw.uri
    assert first_raw.content_hash == second_raw.content_hash
    assert first.snapshot_id != second.snapshot_id


@pytest.mark.parametrize("raw_target", [False, True])
def test_macro_hash_drift_fails_closed(store, registry, monkeypatch, macro_type, raw_target) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=fred_transport())
    result = service.collect(request())
    target = registry.get(result.raw_snapshot_id if raw_target else result.snapshot_id)
    Path(target.uri.removeprefix("file://")).write_bytes(b"{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        service.analyze(result.snapshot_id)


def test_macro_wrong_artifact_type_is_rejected(store, registry, macro_type) -> None:
    wrong = registry.register(ArtifactEnvelope(artifact_type=ArtifactType.DATA_SNAPSHOT, producer="test"))
    with pytest.raises(ValueError, match="MACRO_SNAPSHOT"):
        MacroService(store, registry).analyze(wrong.artifact_id)


def test_macro_service_cutoff_filters_current_download(store, registry, monkeypatch, macro_type) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=fred_transport())
    result = service.collect(request())
    excluded = service.analyze(result.snapshot_id, knowledge_cutoff=FETCHED_AT - timedelta(microseconds=1))
    assert excluded["indicators"] == {}
    assert excluded["observations"] == []
    assert excluded["evidence_entries"] == []
    visible = service.analyze(result.snapshot_id, knowledge_cutoff=FETCHED_AT + timedelta(seconds=1))
    assert visible["coverage"]["DGS10"]["count"] == 2
    with pytest.raises(ValueError, match="timezone-aware"):
        service.analyze(result.snapshot_id, knowledge_cutoff=datetime(2026, 10, 17))


def test_macro_each_response_has_actual_fetch_availability(store, registry, monkeypatch, macro_type) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    ticks = iter(FETCHED_AT + timedelta(seconds=index) for index in range(4))
    service = MacroService(store, registry, clock=lambda: next(ticks), transport=fred_transport())
    result = service.collect(request())
    assert result.as_of == FETCHED_AT + timedelta(seconds=2)
    assert result.available_at == FETCHED_AT + timedelta(seconds=1)
    analyzed = service.analyze(result.snapshot_id)
    assert all(
        datetime.fromisoformat(row["available_at"]) == result.available_at for row in analyzed["observations"]
    )


def test_macro_future_requested_end_is_rejected(store, registry, macro_type) -> None:
    transport = fred_transport()
    with pytest.raises(ValueError, match="collection date"):
        MacroService(store, registry, clock=lambda: FETCHED_AT, transport=transport).collect(
            MacroCollectRequest(series_ids=["DGS10"], end_date=date(2026, 10, 18)),
        )
    assert not transport.calls
    assert not registry.list_all()


def test_macro_cutoff_between_pages_preserves_only_facts_actually_fetched(
    store, registry, monkeypatch, macro_type
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    transport = OfflineTransport(
        {
            f"{FRED_OBSERVATIONS_URL}:0": fred_page(0, 2, [("2026-10-15", "4")]),
            f"{FRED_OBSERVATIONS_URL}:1": fred_page(1, 2, [("2026-10-16", "4.1")]),
        }
    )
    ticks = iter(FETCHED_AT + timedelta(seconds=index) for index in range(4))
    service = MacroService(
        store,
        registry,
        clock=lambda: next(ticks),
        transport=transport,
        config=MacroSourceConfig(fred_page_size=1),
    )
    result = service.collect(request())
    analyzed = service.analyze(result.snapshot_id, knowledge_cutoff=FETCHED_AT + timedelta(seconds=1))
    assert result.as_of == FETCHED_AT + timedelta(seconds=3)
    assert result.available_at == FETCHED_AT + timedelta(seconds=2)
    assert analyzed["coverage"]["DGS10"]["count"] == 1
    assert analyzed["indicators"]["DGS10"]["latest_value"] == 4
    assert analyzed["observations_metadata"]["excluded_unavailable"] == 1
    assert datetime.fromisoformat(analyzed["evidence_entries"][0]["available_at"]) == FETCHED_AT + timedelta(
        seconds=1
    )


@pytest.mark.parametrize("defect", ["response_time", "raw_response_hash", "series_binding", "raw_parent"])
def test_macro_valid_content_hash_does_not_hide_broken_provenance(
    store,
    registry,
    monkeypatch,
    macro_type,
    defect,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-secret")
    service = MacroService(store, registry, clock=lambda: FETCHED_AT, transport=fred_transport())
    result = service.collect(request())
    snapshot_artifact = registry.get(result.snapshot_id)
    raw_artifact = registry.get(result.raw_snapshot_id)
    snapshot = store.get_json(snapshot_artifact.uri)
    raw = store.get_json(raw_artifact.uri)
    if defect == "raw_parent":
        parents = ()
    else:
        if defect == "response_time":
            raw["responses"][0]["ingested_at"] = (FETCHED_AT - timedelta(seconds=1)).isoformat()
        elif defect == "raw_response_hash":
            raw["responses"][0]["content_hash"] = "0" * 64
        else:
            raw["responses"][0]["request_parameters"]["series_id"] = "DFII10"
        raw_uri, raw_digest = store.put_content_addressed_json("macro/raw", raw)
        replacement_raw = registry.register(
            raw_artifact.model_copy(
                update={
                    "artifact_id": "replacement-raw-" + defect,
                    "uri": raw_uri,
                    "content_hash": raw_digest,
                }
            )
        )
        snapshot["raw_snapshot_id"] = replacement_raw.artifact_id
        parents = (replacement_raw.artifact_id,)
    uri, digest = store.put_content_addressed_json("macro/snapshots", snapshot)
    replacement = registry.register(
        snapshot_artifact.model_copy(
            update={
                "artifact_id": "replacement-macro-" + defect,
                "uri": uri,
                "content_hash": digest,
                "parent_artifact_ids": parents,
                "data_snapshot_id": snapshot["raw_snapshot_id"],
            }
        )
    )
    with pytest.raises(ValueError, match="binding|provenance|hash mismatch"):
        service.analyze(replacement.artifact_id)
